"""Archive explicitly started Feishu experiments under verified member wiki nodes."""
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from archive_visibility import VisibilityError, restrict_and_verify

WIKI_BASE = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?(?:feishu\.cn|larksuite\.com)/wiki/?"
)
TOKEN = re.compile(r"[a-zA-Z0-9]{12,64}")
START = re.compile(r"^(?:开始实验记录|开始记录|新建实验记录)(?:[\s：:，]+(.{1,100}))?$")
HELP = ("发「开始记录 实验名称」新建一份草稿，随后逐条口述。发「查看记录」回看，"
        "发「记录完成」会写入你在「实验记录」知识库的成员目录，原有读者可以看到。"
        "发「取消记录」可丢弃未提交的草稿。")
TZ = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger("lab_assistant.archive")
SUMMARY_FIELDS = {
    "实验名称": ("实验名称", "实验名"),
    "实验日期": ("实验日期", "日期"),
    "操作者": ("操作者", "操作人", "实验者"),
    "实验目的": ("实验目的", "目的"),
    "样品及对照": ("样品", "样品编号", "样品及对照", "分组", "对照"),
    "计划": ("计划", "实验计划", "计划操作", "计划量"),
    "实际操作": ("实际", "实际操作", "操作步骤"),
    "测量": ("测量", "读数", "实测", "实测值", "测量值"),
    "观察与结果": ("观察", "结果", "实验结果", "现象", "偏差"),
    "解释": ("解释", "实验者判断"),
    "原始文件索引": ("原始文件索引", "原始文件", "文件名", "文件"),
    "更正": ("更正", "修正"),
    "待补项": ("待补", "待补项"),
}
SUMMARY_LABELS = {label: field for field, labels in SUMMARY_FIELDS.items() for label in labels}
FIELD_SEPARATOR = re.compile(r"[；;\r\n]+|[，,。](?=\s*(?:" + "|".join(SUMMARY_LABELS) + r")\s*[：:])")


class ArchiveError(Exception):
    pass


class Rejected(ArchiveError):
    """Feishu explicitly rejected a request before it could mutate the document."""


class Wiki:
    def __init__(self, client):
        self.client = client
        self.app_id = None
        self.space_id = None

    def request(self, verb, path, *, query=None, body=None):
        from lark_oapi import AccessTokenType, BaseRequest, HttpMethod
        req = (BaseRequest.builder().http_method(HttpMethod[verb]).uri(path)
               .token_types({AccessTokenType.TENANT}).queries(list((query or {}).items())))
        if body is not None:
            req.body(body)
        for attempt in range(3):
            response = self.client.request(req.build())
            limited = response.raw.status_code == 429 or response.code == 99991400
            if not limited or attempt == 2:
                break
            time.sleep(2 ** attempt)
        if not response.success():
            code = response.code
            LOG.warning("Feishu wiki/docx API%s failed: %s", " " + verb, code)
            if response.raw.status_code in (401, 403, 429) or code in (99991400, 99991672, 99991679, 131006, 131005, 131012, 1770032):
                raise Rejected("飞书尚未允许机器人在你的目录写入，或当前请求受限。")
            raise ArchiveError("飞书未确认这次请求。")
        payload = json.loads(response.raw.content)
        return payload.get("data", payload)

    def owns(self, node):
        identity = self.request("GET", "/open-apis/bot/v3/info")
        own = {self.app_id, identity.get("bot", {}).get("open_id")}
        own.discard(None)
        return any(node.get(field) in own for field in ("node_creator", "creator"))


    def pages(self, path, query, field):
        seen = set()
        while True:
            data = self.request("GET", path, query=query)
            yield from data[field]
            if not data.get("has_more"):
                return
            page = data.get("page_token")
            if not page or page in seen:
                raise ArchiveError("飞书目录分页结果不完整。")
            seen.add(page)
            query = {**query, "page_token": page}

    def verify_member_page(self, parent, member_root):
        node = self.request("GET", "/open-apis/wiki/v2/spaces/get_node", query={"token": parent})["node"]
        if (node.get("node_token") != parent or str(node.get("space_id")) != self.space_id
                or node.get("parent_node_token") != member_root or node.get("node_type") != "origin"):
            raise ArchiveError("个人目录已移动或不属于配置的成员目录，请管理员核对。")

    def nodes(self, parent):
        return self.pages(f"/open-apis/wiki/v2/spaces/{self.space_id}/nodes",
                          {"parent_node_token": parent, "page_size": "50"}, "items")

    def create(self, parent, title):
        return self.request("POST", f"/open-apis/wiki/v2/spaces/{self.space_id}/nodes", body={
            "obj_type": "docx", "node_type": "origin", "parent_node_token": parent, "title": title
        })["node"]

    def limit_sharing(self, node, doc):
        restrict_and_verify(self, node, doc)

    def current(self, doc):
        base = f"/open-apis/docx/v1/documents/{doc}"
        rev = self.request("GET", base)["document"]["revision_id"]
        blocks = self.pages(f"{base}/blocks/{doc}/children",
                            {"document_revision_id": str(rev), "page_size": "100"}, "items")
        lines = []
        for block in blocks:
            if block["block_type"] != 2 or block.get("children"):
                raise ArchiveError("文档有其他编辑，请在飞书核对。")
            elements = block["text"]["elements"]
            if any("text_run" not in element for element in elements):
                raise ArchiveError("文档内容已有其他编辑，请在飞书核对。")
            line = "".join(e["text_run"]["content"] for e in elements)
            if line:
                lines.append(line)
        return rev, lines

    def append(self, doc, revision, key, offset, lines):
        blocks = [{"block_type": 2, "text": {"elements": [{"text_run": {"content": line}}]}} for line in lines]
        return self.request("POST", f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/children",
                            query={"document_revision_id": str(revision),
                                   "client_token": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}/{offset}"))},
                            body={"index": -1, "children": blocks})


class Archive:
    def __init__(self, state, wiki, routes_path: Path, app, tenant):
        self.state, self.wiki = state, wiki
        self.routes = {}
        self.wiki_base = self.space_id = self.member_root = None
        if routes_path.exists():
            cfg = json.loads(routes_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict) or not app or not tenant or (
                cfg.get("app_id"), cfg.get("tenant_key")) != (app, tenant):
                raise ValueError("归档路由不属于当前飞书应用或租户")
            self.space_id = cfg.get("space_id")
            self.member_root = cfg.get("member_root")
            base = cfg.get("wiki_base_url")
            if not isinstance(self.space_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", self.space_id):
                raise ValueError("space_id 须为飞书知识空间的数字 ID 字符串")
            if not isinstance(self.member_root, str) or not TOKEN.fullmatch(self.member_root):
                raise ValueError("member_root 须为成员目录的知识库节点 token")
            if not isinstance(base, str) or not WIKI_BASE.fullmatch(base):
                raise ValueError("wiki_base_url 须为飞书或 Lark 租户 HTTPS 地址下的 /wiki/ 路径")
            self.wiki_base = base.rstrip("/") + "/"
            self.routes = cfg.get("routes")
            if not isinstance(self.routes, dict):
                raise ValueError("routes 须为已核实的身份与个人目录的映射")
            owners = set()
            names = {}
            for identity, route in self.routes.items():
                if not re.fullmatch(r"(?:open_id|union_id|user_id):[A-Za-z0-9_-]+", identity):
                    raise ValueError("归档成员身份无效")
                if (not isinstance(route, dict) or not isinstance(route.get("parent"), str)
                        or not TOKEN.fullmatch(route["parent"]) or route["parent"] == self.member_root
                        or not isinstance(route.get("name"), str) or not route["name"].strip()):
                    raise ValueError("归档成员目录无效")
                kind = identity.split(":", 1)[0]
                parent, name = route["parent"], route["name"]
                if (kind, parent) in owners or name != names.setdefault(parent, name):
                    raise ValueError("同一成员目录存在冲突的身份或姓名")
                owners.add((kind, parent))
            wiki.space_id, wiki.app_id = self.space_id, app
        with state.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS experiments(
                id TEXT PRIMARY KEY, user TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
                title TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'draft', node TEXT, doc TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS one_active ON experiments(user) WHERE phase != 'saved';
              CREATE TABLE IF NOT EXISTS experiment_notes(
                message TEXT PRIMARY KEY, experiment TEXT NOT NULL, stamp TEXT NOT NULL, content TEXT NOT NULL);
            """)

    def route(self, sender):
        found = {tuple(self.routes[key][f] for f in ("parent", "name"))
                 for kind in ("open_id", "user_id", "union_id")
                 if (value := getattr(sender, kind, None))
                 and (key := f"{kind}:{value}") in self.routes}
        return next(iter(found))[0] if len(found) == 1 else None

    def active(self, user):
        with self.state.db() as db:
            return db.execute("SELECT * FROM experiments WHERE user=? AND phase!='saved'", (user,)).fetchone()

    def note(self, row):
        with self.state.db() as db:
            record = db.execute("SELECT id, parent FROM experiments WHERE user=? AND phase='draft'", (row["user"],)).fetchone()
            if not record or row["archive_parent"] != record["parent"]:
                return False
            when = datetime.fromtimestamp(row["created"], TZ).strftime("%Y-%m-%d %H:%M")
            db.execute("INSERT OR IGNORE INTO experiment_notes VALUES(?,?,?,?)",
                       (row["id"], record["id"], when, row["input"]))
        return True

    def entries(self, key):
        with self.state.db() as db:
            return db.execute("SELECT stamp,content FROM experiment_notes WHERE experiment=? ORDER BY rowid", (key,)).fetchall()

    def lines(self, record, notes):
        result = [record["title"], "成员目录 · " + record["name"], "实验口述与更正（北京时间，按发送顺序）"]
        values = {field: [] for field in SUMMARY_FIELDS}
        for i, entry in enumerate(notes, 1):
            result.append(f"{i}. {entry['stamp']}")
            for line in entry["content"].splitlines():
                result.extend(line[pos:pos+1000] for pos in range(0, len(line), 1000))
            for clause in FIELD_SEPARATOR.split(entry["content"]):
                pair = re.split(r"[：:]", clause.strip(), maxsplit=1)
                if len(pair) == 2 and (field := SUMMARY_LABELS.get(pair[0].strip())) and pair[1].strip():
                    values[field].append(clause.strip())

        # Preserve the previous transcript as a prefix so a partially written document can resume.
        result.extend(["固定字段摘要（按口述顺序，补充与更正见原文）",
                       "记录成员（飞书账号）：" + record["name"],
                       "首次口述日期（北京时间）：" + (notes[0]["stamp"][:10] if notes else "待补")])
        if not values["实验名称"] and record["title"] != "实验记录":
            values["实验名称"].append("实验名称：" + record["title"])
        for field, entries in values.items():
            if not entries:
                result.append(field + "：待补")
                continue
            for entry in entries:
                result.extend(entry[pos:pos+1000] for pos in range(0, len(entry), 1000))
        return result

    def command(self, row):
        user, text, parent = row["user"], row["input"].strip().rstrip("。！？!"), row["archive_parent"]
        start = START.fullmatch(text)
        if not start and text not in {"归档说明", "如何归档", "实验记录目录", "查看记录", "取消记录", "记录完成", "结束记录", "保存实验记录"}:
            return None
        if self.wiki_base is None:
            return "归档尚未配置。管理员可在本机配置知识库与成员目录。"
        directory = self.wiki_base + self.member_root
        if text in {"归档说明", "如何归档", "实验记录目录"}:
            return HELP + "\n" + directory
        if not parent:
            return "你的飞书身份尚未与个人实验目录核对。请联系管理员补齐映射。\n" + directory
        current = self.active(user)
        if current and current["parent"] != parent:
            return "个人目录映射已变更，请管理员核对原草稿的目录。"
        if start:
            if current:
                return "你已有一份正在处理的记录：" + current["title"] + "。\n" + HELP
            title = start[1] or "实验记录"
            name = next(route["name"] for route in self.routes.values() if route["parent"] == parent)
            ident = uuid.uuid4().hex
            with self.state.db() as db:
                db.execute("INSERT INTO experiments(id,user,parent,name,title) VALUES(?,?,?,?,?)", (ident,user,parent,name,title))
            return f"已开始「{title}」。请逐条口述实际操作、测量或更正。收到「记录完成」后会保存至「{name}｜实验记录」，沿用目录的阅读权限。"
        if not current:
            if text in {"记录完成", "结束记录", "保存实验记录"}:
                with self.state.db() as db:
                    last = db.execute("SELECT node FROM experiments WHERE user=? AND parent=? AND phase='saved' ORDER BY rowid DESC LIMIT 1", (user,parent)).fetchone()
                if last:
                    return "当前没有进行中的记录。上次已归档的文档：\n" + self.wiki_base + last["node"]
            return "当前没有进行中的记录。\n" + HELP
        key = current["id"]
        if text == "取消记录":
            if current["phase"] != "draft":
                return "飞书归档已经开始，请发「记录完成」核对同一文档。"
            with self.state.db() as db:
                db.execute("DELETE FROM experiment_notes WHERE experiment=?", (key,))
                db.execute("DELETE FROM experiments WHERE id=?", (key,))
            return "已取消本次草稿。"
        notes = self.entries(key)
        if text == "查看记录":
            body = "\n".join(self.lines(current, notes))
            return body if len(body) < 7000 else body[:6800] + "\n（预览已截短。归档包含全部口述。）"
        if not notes:
            return "这份记录还没有口述内容。请先发一条实际观察或模拟测量，再发「记录完成」。"
        try:
            return self.save(current, notes)
        except Exception as err:
            LOG.warning("保存待复核 (%s)", type(err).__name__)
            reason = str(err) if isinstance(err, (ArchiveError, VisibilityError)) else "飞书尚未返回完整的归档结果。"
            return reason + "\n本地草稿已保留。请稍后再发「记录完成」核对同一份记录。"

    def save(self, record, notes):
        key, parent = record["id"], record["parent"]
        self.wiki.verify_member_page(parent, self.member_root)
        title = f"实验记录 · {notes[0]['stamp'][:10]} · {key[:12]}"
        node, doc = record["node"], record["doc"]
        if not node:
            matches = [n for n in self.wiki.nodes(parent) if n["title"] == title]
            if len(matches) > 1:
                raise ArchiveError("目录中有同名文档，请管理员核对。")
            if matches:
                if record["phase"] != "creating" or not self.wiki.owns(matches[0]):
                    raise ArchiveError("同名文档的创建身份无法确认，请管理员核对。")
                created = matches[0]
            else:
                if record["phase"] == "creating":
                    raise ArchiveError("建页请求已发出，尚未在目标目录找到对应文档，请管理员核对。")
                with self.state.db() as db:
                    db.execute("UPDATE experiments SET phase='creating' WHERE id=?", (key,))
                try:
                    created = self.wiki.create(parent, title)
                except Rejected:
                    with self.state.db() as db:
                        db.execute("UPDATE experiments SET phase='draft' WHERE id=?", (key,))
                    raise
            node, doc = created["node_token"], created["obj_token"]
            if not (TOKEN.fullmatch(node) and TOKEN.fullmatch(doc) and created["parent_node_token"] == parent
                    and created["obj_type"] == "docx" and created.get("node_type") == "origin"
                    and str(created.get("space_id")) == self.space_id):
                raise ArchiveError("新文档身份或位置无法核实。")
            with self.state.db() as db:
                db.execute("UPDATE experiments SET phase='writing',node=?,doc=? WHERE id=?", (node,doc,key))
        # Re-check the actual parent on every attempt, including after a delayed retry.
        if not any(n["node_token"] == node and n["obj_token"] == doc and n.get("node_type") == "origin"
                   and n.get("parent_node_token") == parent and str(n.get("space_id")) == self.space_id
                   for n in self.wiki.nodes(parent)):
            raise ArchiveError("文档位置已变更或飞书索引仍在更新，请核对后重试。")
        self.wiki.limit_sharing(node, doc)
        expected = self.lines(record, notes)
        while True:
            revision, have = self.wiki.current(doc)
            if have == expected:
                break
            if have != expected[:len(have)]:
                raise ArchiveError("飞书文档正文已被其他人修改，请在飞书核对。")
            self.wiki.append(doc, revision, key, len(have), expected[len(have):len(have)+30])
            _, readback = self.wiki.current(doc)
            if len(readback) <= len(have):
                raise ArchiveError("飞书尚未确认正文写入。")
        with self.state.db() as db:
            db.execute("UPDATE experiments SET phase='saved' WHERE id=?", (key,))
            db.execute("DELETE FROM experiment_notes WHERE experiment=?", (key,))
        return f"已归档到「{record['name']}｜实验记录」，已核对 {len(notes)} 条口述。\n" + self.wiki_base + node
