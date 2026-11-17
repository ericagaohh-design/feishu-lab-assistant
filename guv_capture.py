"""Extract recorded phrases for a versioned GUV imaging form, without model inference."""
import json
import re
from pathlib import Path

TEMPLATE_FILE = Path(__file__).with_name("guv_imaging_template.json")
START_IMAGING = re.compile(r"^开始\s*GUV\s*成像记录(?:[\s：:，]+(.{1,100}))?$", re.I)
IMAGING_TITLE = re.compile(r"GUV.{0,12}(?:成像|显微|拍照|视野)|(?:成像|显微|拍照|视野).{0,12}GUV", re.I)
FILENAME = re.compile(r"[^\s，,。；;、：:“”「」]+\.(?:ome\.tiff?|tiff?|czi|lif|nd2|oib|oif|lsm|vsi|oir|png|jpe?g)(?![.A-Za-z0-9])", re.I)
SAMPLE = re.compile(r"(?:[A-Za-z0-9][\w.-]{0,24}\s*(?:号)?(?:样品|样本|组)|(?:样品|样本|批次|批号)(?:编号|号)?(?:为|是|叫)?\s*[A-Za-z0-9][A-Za-z0-9_.-]*)", re.ASCII)
LOCATION = re.compile(r"(?:[0-9一二两三四五六七八九十]+\s*个?\s*视野|(?:第\s*)?[0-9一二两三四五六七八九十]+\s*号?视野|视野\s*[A-Za-z0-9]+|(?<![A-Za-z0-9_.-])F\d{1,3}(?![A-Za-z0-9_.-])|(?:[A-H]\d{1,2}|\d+号)\s*孔|孔(?:位|号)\s*[A-H]?\d+|中心[区域]?|边缘[区域]?)", re.I)
ACQUISITION = re.compile(r"明场|暗场|共聚焦|相差|荧光(?:通道|模式|成像)|(?:物镜|镜头|通道|比例尺|像素尺寸)|\d+\s*[xX×倍]\s*物镜")
OBSERVATION = re.compile(r"观察到|看到|看见|没看到|未见|数到|数了|计数|测得|实测|读数|测了|聚集|聚团|团聚|成团|黏连|粘连|堆叠|重叠|破裂|明显|(?:直径|尺寸)\s*(?:约|为|是)?\s*\d")
COUNT_NUMBER = r"[0-9零〇一二两三四五六七八九十百千万]+"
VESICLE_COUNT = re.compile(
    rf"(?:{COUNT_NUMBER}\s*(?:个|颗|粒)\s*(?:囊泡|GUV)(?![A-Za-z0-9_]|\s*(?:样品|样本|视野|文件))|"
    rf"(?:囊泡|(?<![A-Za-z0-9_])GUV)(?:\s*(?:数量|个数|总数))?\s*(?:有|为|是|共计|共)?\s*"
    rf"{COUNT_NUMBER}\s*(?:个|颗|粒)(?!\s*(?:视野|孔|管|样品|样本|文件)))", re.I)
PENDING = re.compile(r"待补|待测|待填|待定|待确认|不清楚|还没|还未|尚未|回头.{0,8}补|回工位.{0,8}补|随后补|稍后补|没记|记不清|不知道")
PLAN = re.compile(r"打算|准备|接下来|下一步|计划|预计|明天|将要|想要|拟(?:拍|用|做|按|以|计数|统计)|预期")
HYPOTHESIS = re.compile(r"如果|假如|假设|或许|也许|可能|猜测|怀疑|大概是因为|应该是|推测|似乎是因为|我觉得")
CORRECTION = re.compile(r"更正|纠正|修正|口误|说错|应(?:该)?改为|改成|应该是.+(?:才对|不是)")
ACTUAL = re.compile(r"^(?:但|不过)?\s*(?:本次|这次|实际|已经|刚才|刚刚|实测|拍到了|观察到)")
IMAGE_BASENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:ome\.tiff?|tiff?|czi|lif|nd2|oib|oif|lsm|vsi|oir|png|jpe?g)(?![.A-Za-z0-9])", re.I)
DEFERRED_TOPICS = {"files": re.compile(r"原图|文件|路径"), "location": re.compile(r"视野|孔位"),
                   "acquisition": re.compile(r"物镜|仪器|成像|设置"), "sample": re.compile(r"样品|批次"),
                   "observation": re.compile(r"观察|计数|读数"), "linking": re.compile(r"对应|关联")}
FILE_REFERENCE = re.compile(r"文件夹|文件名|原图(?:叫|是|名为|存于|存在|位于)|(?:原图|文件).{0,6}(?:目录|路径)|(?:https?://|/)[^\s，。；]+")
LINK_WORD = re.compile(r"对应|属于|来自|的(?:原图|文件)|文件名|原图叫|存为|保存为")


def load_template():
    return json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))


def template_name(template):
    return f"{template['title']} v{template['version']}"


def template_text():
    template = load_template()
    fields = [f"{field['label']} — {field['hint']}" for field in template["fields"]]
    lines = [template_name(template) + "（草案）", *fields,
             "发「开始GUV成像记录 实验名称」后可以直接口述，发「查看记录」核对。"]
    if template.get("discussion_url"):
        lines.append("讨论稿：" + template["discussion_url"])
    return "\n".join(lines)


def extract_fields(template, texts):
    """Return field ID -> ordered (one-based dictation number, exact utterance fragment)."""
    aliases = {alias: field["id"] for field in template["fields"] for alias in [field["label"], *field["aliases"]]}
    fields = {field["id"]: [] for field in template["fields"]}
    for number, text in enumerate(texts, 1):
        for match in re.finditer(r"([^；;。！？!?\n]+)([；;。！？!?\n]?)", text):
            sentence, end = match.groups()
            if end in {"?", "？"} or sentence.rstrip().endswith(("吗", "么")):
                if "pending" in fields:
                    fields["pending"].append((number, sentence.strip() + end))
                continue
            context = None
            for clause in re.split(r"[，,](?!\d{3}(?:\D|$))", sentence):
                clause = clause.strip()
                if not clause:
                    continue
                pair = re.match(r"^([^：:]{1,25})[：:]\s*(.+)$", clause)
                label = aliases.get(pair[1].strip()) if pair else None
                if label or ACTUAL.match(clause):
                    context = None
                if CORRECTION.search(clause) or label == "correction":
                    context = "correction"
                elif PENDING.search(clause) or label == "pending":
                    context = "pending"
                elif PLAN.search(clause) or label == "plan":
                    context = "plan"
                elif HYPOTHESIS.search(clause) or label == "interpretation":
                    context = "interpretation"
                elif re.match(r"^(?:上次|上批|上一批|上一组|文献中|据文献)", clause):
                    context = "background"
                if context:
                    tags = {context}
                else:
                    tags = {label} if label else set()
                    if not label:
                        for key, regex in (("sample", SAMPLE), ("location", LOCATION),
                                           ("acquisition", ACQUISITION)):
                            if regex.search(clause):
                                tags.add(key)
                        observable = FILENAME.sub("", clause)
                        if OBSERVATION.search(observable) or VESICLE_COUNT.search(observable):
                            tags.add("observation")
                        if FILENAME.search(clause) or FILE_REFERENCE.search(clause):
                            tags.add("files")
                    if ("files" in tags and (SAMPLE.search(clause) or LOCATION.search(clause))
                            and (LINK_WORD.search(clause) or FILENAME.search(clause))):
                        tags.add("linking")
                for field_id in tags.intersection(fields):
                    fields[field_id].append((number, clause))
    return fields


def missing_question(template, fields):
    deferred = "；".join(clause for _, clause in fields.get("pending", []))
    for field in template["fields"]:
        key = field["id"]
        if not field.get("ask"):
            continue
        if key == "linking" and not DEFERRED_TOPICS[key].search(deferred):
            named_files = {name for _, phrase in fields.get("files", []) for name in IMAGE_BASENAME.findall(phrase)}
            linked_files = {name for _, phrase in fields[key] for name in IMAGE_BASENAME.findall(phrase)}
            missing_files = sorted(named_files - linked_files)
            if missing_files:
                return f"原图 {missing_files[0]} 对应哪个样品或视野？可以连文件名一起说。"
        if fields[key]:
            continue
        if key == "linking" and not fields.get("files"):
            continue
        if key in DEFERRED_TOPICS and DEFERRED_TOPICS[key].search(deferred):
            continue
        return field["ask"]
    return ""


def summary_lines(template, fields):
    lines = [template_name(template) + "（逐项口述摘录，更正另列）"]
    for field in template["fields"]:
        entries = fields[field["id"]]
        if entries:
            lines.append(field["label"])
            for number, clause in entries:
                value = f"〔第{number}条〕{clause}"
                lines.extend(value[offset:offset+1000] for offset in range(0, len(value), 1000))
        elif field.get("ask") or field["id"] in ("date", "operator"):
            lines.append(field["label"] + "：待补")
    return lines
