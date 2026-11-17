"""Read-only review of a member's own dictations and verified archive pages."""
import json
import logging
import re
from dataclasses import dataclass

from archive import FIELD_SEPARATOR, SUMMARY_LABELS, TOKEN
from guv_capture import ACTUAL, HYPOTHESIS, PENDING, PLAN, extract_fields, load_template

LOG = logging.getLogger("lab.review")
REVIEW = {"复盘本次实验", "复盘这次实验", "复盘本次记录", "实验复盘"}
COMPARE = {"对比上次实验", "对比上一次实验", "和上次实验对比", "对比上次记录"}
TRANSCRIPT = "实验口述与更正（北京时间，按发送顺序）"
SUMMARY = "固定字段摘要（按口述顺序，补充与更正见原文）"
STAMP = re.compile(r"(\d+)\. \d{4}-\d\d-\d\d \d\d:\d\d")
# Use identical names for explicit labels and the corresponding dictated fields.
IMAGING_FIELDS = {"sample": "样品及对照", "location": "观察位置与视野", "acquisition": "采集条件",
                  "observation": "观察与结果", "files": "原始文件索引", "linking": "样品与文件对应",
                  "correction": "更正", "plan": "计划", "interpretation": "解释", "pending": "待补项",
                  "date": "实验日期", "operator": "操作者", "purpose": "实验目的", "background": "记录背景"}
DISPLAY_ORDER = ("更正", "样品及对照", "测量", "观察与结果", "观察位置与视野", "采集条件", "实际操作",
                 "原始文件索引", "样品与文件对应", "计划", "解释", "待补项", "实验目的", "实验日期", "操作者", "记录背景")

# Only denominators actually named in a count are eligible for comparison.
COUNT_BASIS = re.compile(r"(?:每(?:一)?(?:个|支)?\s*|[/／]\s*)(视野|孔|管)")
COUNT_MENTION = re.compile(r"计数|统计(?:分母|口径)|数到|数了|数得|[0-9一二两三四五六七八九十]+(?:\.\d+)?\s*(?:颗|粒|个(?!视野|孔|管|文件|图像))")
UNSET_BASIS = re.compile(r"待定|待确认|待补|不清楚|不知道|还没|尚未|是否|还是|可能|计划|准备|[？?]")
REVISED_BASIS = re.compile(r"更正|口误|说错|修正|改为|改成|不再按|并非|而非|不是")


class ReviewUnavailable(Exception):
    pass


@dataclass
class Snapshot:
    title: str
    texts: list[str]
    source: str
    template: dict | None


def _transcript(record, lines):
    """Only read the original dictation section of a page produced by this bot."""
    head = [record["title"], "成员目录 · " + record["name"], TRANSCRIPT]
    if lines[:3] != head:
        raise ReviewUnavailable("归档页的口述区标题已变更，请在飞书核对原文。")
    # Older archives end after the transcript, before the field summary was introduced.
    stops = [i for i in range(3, len(lines)-2) if lines[i:i+2] == [SUMMARY, "记录成员（飞书账号）：" + record["name"]]
             and lines[i+2].startswith("首次口述日期（北京时间）：")]
    if len(stops) > 1 or (SUMMARY in lines and not stops):
        raise ReviewUnavailable("归档页的口述区边界无法确认，请核对原文。")
    notes = []
    for line in lines[3:stops[0] if stops else len(lines)]:
        stamp = STAMP.fullmatch(line)
        if stamp:
            if int(stamp[1]) != len(notes) + 1:
                raise ReviewUnavailable("归档页的口述编号已变更，请核对原文。")
            notes.append([])
        elif notes:
            notes[-1].append(line)
        else:
            raise ReviewUnavailable("归档页的口述编号未找到，请核对原文。")
    if any(not note for note in notes):
        raise ReviewUnavailable("归档页有空白口述，请核对原文。")
    return ["\n".join(note) for note in notes]


def _read(archive, record, wiki_base, space_id):
    template = json.loads(record["capture_template"]) if record["capture_template"] else None
    if record["phase"] != "saved":
        notes = archive.entries(record["id"])
        return Snapshot(record["title"], [entry["content"] for entry in notes], "当前归档草稿", template)
    parent, node, doc = (record[key] for key in ("parent", "node", "doc"))
    if not all(isinstance(v, str) and TOKEN.fullmatch(v) for v in (parent, node, doc)):
        raise ReviewUnavailable("归档编号无效，请核对原文。")

    def check_location():
        archive.wiki.verify_member_page(parent, archive.member_root)
        actual = archive.wiki.request("GET", "/open-apis/wiki/v2/spaces/get_node",
                                      query={"token": node})["node"]
        if (actual.get("node_token"), actual.get("obj_token"), actual.get("parent_node_token"),
            actual.get("node_type"), actual.get("obj_type"), str(actual.get("space_id"))) != (
                node, doc, parent, "origin", "docx", str(space_id)):
            raise ReviewUnavailable("归档页已移动或无法核对本人目录，请核对后再复盘。")

    check_location()
    revision, lines = archive.wiki.current(doc)
    check_location()
    return Snapshot(record["title"], _transcript(record, lines), f"{wiki_base}{node}（文档版本 {revision}）", template)


def _fields(snapshot):
    texts = snapshot.texts
    extracted = extract_fields(snapshot.template or load_template(), texts)
    guarded = {n: [] for n in range(1, len(texts) + 1)}
    for key in ("plan", "interpretation", "correction", "pending"):
        for n, value in extracted.get(key, []):
            guarded[n].append((IMAGING_FIELDS[key], value))
    result = {}
    for index, text in enumerate(texts, 1):
        for clause in FIELD_SEPARATOR.split(text):
            pair = re.split(r"[：:]", clause.strip(), maxsplit=1)
            if len(pair) == 2 and pair[1].strip() and (name := SUMMARY_LABELS.get(pair[0].strip())):
                # A clause like “测量：待测” records a pending item, not a measured value.
                if not any(label != name and value in clause for label, value in guarded[index]):
                    result.setdefault(name, []).append((index, clause.strip()))
    for key, items in extracted.items():
        name = IMAGING_FIELDS.get(key)
        if not name or not items:
            continue
        group = result.setdefault(name, [])
        for item in items:
            if not (1 <= item[0] <= len(texts) and item[1] and item[1] in texts[item[0]-1]):
                continue
            # A labelled clause already carries the user's wording and its source.
            if not any(n == item[0] and (s in item[1] or item[1] in s) for n, s in group):
                group.append(item)
        group.sort(key=lambda item: item[0])
        if not group:
            del result[name]
    return result


def _list_evidence(entries, label):
    excerpts = []
    selected = [entries[0], entries[-1]] if len(entries) > 2 else entries
    for number, value in selected:
        excerpt = value[:80].replace("\n", " ") + ("…" if len(value) > 80 else "")
        excerpts.append(f"「{excerpt}」〔{label}#{number}〕")
    if len(entries) > 2:
        excerpts.append(f"中间 {len(entries)-2} 处见原始口述")
    return "、".join(excerpts)


def _reported_count_phrases(text):
    for sentence in re.finditer(r"([^。；;！？!?\n]+)([。；;！？!?\n]?)", text):
        body, end = sentence.groups()
        if end in {"?", "？"} or body.rstrip().endswith(("吗", "么")):
            continue
        parts = []
        deferred = False
        for clause in re.split(r"(?<=[，,])", body):
            if any(rule.search(clause) for rule in (PLAN, HYPOTHESIS, PENDING, UNSET_BASIS)):
                if parts:
                    yield "".join(parts).strip(" ，,")
                    parts = []
                deferred = True
            elif ACTUAL.match(clause.strip()) or re.match(r"\s*(?:更正|修正|纠正|观察[：:]|结果[：:])", clause):
                deferred = False
            if not deferred:
                parts.append(clause)
        if parts:
            yield "".join(parts).strip(" ，,")


def _count_basis(texts):
    found = {}
    revised = False
    for number, text in enumerate(texts, 1):
        # Keep clauses in the same sentence together so a count can name its denominator later.
        for sentence in _reported_count_phrases(text):
            basis = set(COUNT_BASIS.findall(sentence))
            if not basis:
                continue
            corrected = bool(REVISED_BASIS.search(sentence))
            if not corrected and not COUNT_MENTION.search(sentence):
                continue
            revised |= corrected
            for unit in sorted(basis):
                found.setdefault(unit, []).append((number, sentence.strip()))
    return found, revised


def _compare_count_basis(current, previous):
    a, correction_a = _count_basis(current.texts)
    b, correction_b = _count_basis(previous.texts)
    if not a and not b:
        return []
    if correction_a or correction_b:
        judgement = "口述中有口径更正或排除说明，请先核对最终采用的分母。"
    elif not a or not b:
        absent = "上次" if a else "本次"
        judgement = f"{absent}未识别到计数分母，两份计数暂不能直接比较。"
    elif a.keys() != b.keys() or len(a) != 1:
        judgement = "两份口径不同或存在多种分母，计数不能直接比较，也不能直接算差值。"
    else:
        judgement = "两份都提到相同计数分母，仍需核对样品及取景范围。"
    lines = ["计数口径核对 · " + judgement]
    for label, values in (("本", a), ("上", b)):
        for unit, evidence in values.items():
            lines.append(f"· {label}次出现「每{unit}」口径  " + _list_evidence(evidence, label))
    return lines


def _render(current, previous, compare):
    current_fields = _fields(current)
    lines = [f"本次《{current.title}》 · {len(current.texts)} 条口述", current.source]
    if previous:
        lines += [f"上次《{previous.title}》 · {len(previous.texts)} 条口述", previous.source]
    if not current.texts:
        lines.append("本次还没有口述，可先记一条实际观察。")
        return "\n".join(lines)
    if compare and not previous:
        lines.append("当前没有可供对照的上一份本人归档。")
    if previous:
        prior_fields = _fields(previous)
        lines.append("同名字段原文对照（本次 / 上次）")
        names = [name for name in DISPLAY_ORDER if name in current_fields or name in prior_fields]
        for name in names[:8]:
            a, b = current_fields.get(name, []), prior_fields.get(name, [])
            left = _list_evidence(a, "本") if a else "未识别到对应口述"
            right = _list_evidence(b, "上") if b else "未识别到对应口述"
            if a and b:
                same = [v for _, v in a] == [v for _, v in b]
                difference = "两次原文一致" if same else "两次原文不同"
            else:
                difference = "仅一侧有明确口述"
            lines.append(f"· {name}（{difference}）  {left} / {right}")
        lines.extend(_compare_count_basis(current, previous))
        if len(names) > 8:
            lines.append("其他字段请回看两份原文。")
        if not names:
            lines.append("两份口述尚无可识别的对照字段。")
    else:
        lines.append("本次记录中的证据")
        for name in [n for n in DISPLAY_ORDER if n in current_fields][:8]:
            lines.append(f"· {name}  " + _list_evidence(current_fields[name], "本"))
        if not current_fields:
            lines.append(_list_evidence(list(enumerate(current.texts, 1)), "本"))

    if current_fields.get("更正") or (previous and prior_fields.get("更正")):
        lines.append("记录中包含更正，请对照相应原句确认最终读数。")
    missing = [field for field in ("样品及对照", "观察与结果", "原始文件索引")
               if not current_fields.get(field) and not (field == "观察与结果" and current_fields.get("测量"))]
    if missing:
        lines.append("本次未识别到明确口述的字段：" + "、".join(missing) + "。")
    if previous:
        lines.append("判断边界：以上为原文并列。样品、条件及单位的可比性需核对，原因待验证。")
    else:
        lines.append("判断边界：以上依据成员口述，原因待验证。")
    return "\n".join(lines)


def review_command(archive, row, *, wiki_base, space_id):
    """Return a local reply for a review command, or None for an unrelated message."""
    text = row["input"].strip().rstrip("。！？!?")
    if text not in REVIEW and text not in COMPARE:
        return None
    user, parent = row["user"], row["archive_parent"]
    if not archive or not parent or not user:
        return "请先让管理员核对你的飞书身份和本人目录，再进行实验复盘。"
    direct = archive.routes.get("open_id:" + user)
    if (direct and direct["parent"] != parent) or not any(r["parent"] == parent for r in archive.routes.values()):
        return "成员目录已变更，请核对身份映射后再复盘。"
    compare = text in COMPARE
    try:
        active = archive.active(user)
        if active is not None and active["parent"] != parent:
            raise ReviewUnavailable("进行中的草稿属于旧目录，请先核对成员映射。")
        with archive.state.db() as db:
            saved = db.execute("""SELECT * FROM experiments WHERE user=? AND parent=? AND phase='saved'
                                  ORDER BY rowid DESC LIMIT 2""", (user, parent)).fetchall()
        records = ([active] if active else []) + saved
        if not records:
            return "还没有通过本助手建立的本人记录。发「开始记录 实验名称」后即可口述并复盘。"
        current = _read(archive, records[0], wiki_base, space_id)
        previous = _read(archive, records[1], wiki_base, space_id) if compare and len(records) > 1 else None
        return _render(current, previous, compare)
    except ReviewUnavailable as error:
        return str(error)
    except Exception as error:
        LOG.warning("复盘原文暂时无法核实 (%s)", type(error).__name__)
        return "本次未能读全并核实复盘所需的本人记录，请稍后重试或在飞书查看原文。"
