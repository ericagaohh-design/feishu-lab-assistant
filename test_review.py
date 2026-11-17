"""Only a member's own real archive transcript or active dictation can supply review evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from archive import Archive
from guv_capture import load_template
from review import review_command
from state import State


class ReadOnlyWiki:
    def __init__(self):
        self.pages = {}
        self.reads = []
        self.fail = False
        self.moved_parent = None

    def verify_member_page(self, parent, root):
        assert root == "SyntheticMemberRoot"
        if parent == self.moved_parent:
            raise ValueError("synthetic folder was moved")

    def request(self, method, path, *, query):
        assert (method, path) == ("GET", "/open-apis/wiki/v2/spaces/get_node")
        return {"node": self.pages[query["token"]][0]}

    def current(self, doc):
        self.reads.append(doc)
        if self.fail:
            raise OSError("synthetic read error")
        return 2, next(lines for node, lines in self.pages.values() if node["obj_token"] == doc)


class MemberReviewTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = State(Path(folder.name) / "queue")
        self.wiki = ReadOnlyWiki()
        # The fake only implements the permissions that the review needs.
        self.archive = SimpleNamespace(state=self.state, wiki=self.wiki, member_root="SyntheticMemberRoot")
        self.archive.entries = lambda key: Archive.entries(self.archive, key)
        self.archive.active = lambda user: Archive.active(self.archive, user)
        self.home = "https://example.feishu.cn/wiki/"
        self.parents = {"A": "ParentForAlice01", "B": "ParentForBob0002"}
        self.archive.routes = {"open_id:"+u: {"parent": p, "name": u} for u,p in self.parents.items()}
        self.counter = 0

    def record(self, who, texts, saved=False, template=None):
        self.counter += 1
        ident = str(self.counter)
        node, doc = "WikiSynthetic"+ident, "DocumentSynthetic"+ident
        row = {"name": who, "id": ident, "title": "模拟 "+ident, "parent": self.parents[who]}
        row["capture_template"] = json.dumps(template, ensure_ascii=False) if template else None
        stamp = "2030-01-01 09:00"
        notes = [{"stamp": stamp, "content": text} for text in texts]
        with self.state.db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS experiments(id, user, parent, name, title, phase, node, doc, capture_template)""")
            con.execute("CREATE TABLE IF NOT EXISTS experiment_notes(message,experiment,stamp,content)")
            con.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?)",
                        (ident, who, row["parent"], who, row["title"], "saved" if saved else "draft", node, doc, row["capture_template"]))
            if not saved:
                con.executemany("INSERT INTO experiment_notes VALUES(?,?,?,?)",
                                [(f"{ident}-{i}", ident, stamp, n) for i,n in enumerate(texts)])
        if saved:
            lines = Archive.lines(self.archive, row, notes)
            lines.append("模型摘要里的虚构字段：99 kg")
            self.wiki.pages[node] = ({"node_token":node, "obj_token":doc, "parent_node_token":row["parent"],
                                      "space_id":"1", "node_type":"origin", "obj_type":"docx"}, lines)
        return node, doc

    def run_cmd(self, text, who="A", parent=None):
        return review_command(self.archive, {"input":text, "user":who,
            "archive_parent":parent if parent is not None else self.parents[who]}, wiki_base=self.home, space_id="1")

    def test_draft_reviews_only_own_originals_and_never_calls_remote(self):
        self.record("B", ["观察：他人记录"], saved=True)
        self.record("A", ["样品：SIM-A；测量：1.00 mL", "更正：应为 1.01 mL"])
        reply = self.run_cmd("复盘本次实验")
        self.assertIn("1.00 mL", reply)
        self.assertIn("1.01 mL", reply)
        self.assertIn("本#2", reply)
        self.assertNotIn("他人记录", reply)
        self.assertFalse(self.wiki.reads)
        self.assertIsNone(self.run_cmd("准备成像"))
        self.assertNotIn("1.00", self.run_cmd("复盘本次实验", parent=self.parents["B"]))

    def test_saved_review_is_read_only_with_verified_citations_and_change_scope(self):
        first, d1 = self.record("A", ["样品：SIM-A\n测量：1.0 mL"], True)
        other, d2 = self.record("B", ["测量：私密值"], True)
        latest, d3 = self.record("A", ["样品：SIM-A；测量：200 µL"], True)
        reply = self.run_cmd("对比上次实验")
        self.assertEqual(set(self.wiki.reads), {d1,d3})
        self.assertNotIn(d2, self.wiki.reads)
        for expected in ("样品及对照（两次原文一致）", "测量（两次原文不同）", self.home+first,
                         self.home+latest, "200 µL", "1.0 mL", "上#1"):
            self.assertIn(expected, reply)
        self.assertNotIn("99 kg", reply)
        self.assertNotIn("私密值", reply)

    def test_moved_shortcut_and_failed_remote_do_not_disclose_record(self):
        node, doc = self.record("A", ["观察：不应泄漏"], True)
        self.wiki.pages[node][0]["node_type"] = "shortcut"
        self.assertNotIn("不应泄漏", self.run_cmd("复盘本次实验"))
        self.assertFalse(self.wiki.reads)
        self.wiki.pages[node][0]["node_type"] = "origin"
        self.wiki.fail = True
        self.assertIn("未能读全", self.run_cmd("复盘本次实验"))
        self.assertNotIn("不应泄漏", self.run_cmd("复盘本次实验"))

    def test_moved_member_folder_does_not_fetch_document(self):
        self.record("A", ["观察：合成内容"], saved=True)
        self.wiki.moved_parent = self.parents["A"]
        self.assertNotIn("合成内容", self.run_cmd("复盘本次实验"))
        self.assertFalse(self.wiki.reads)

    def test_imaging_archive_snapshot_and_latest_correction(self):
        template = load_template()
        next(f for f in template["fields"] if f["id"] == "acquisition")["aliases"].append("测试专用设置")
        notes = ["A样品拍了3个视野", "测试专用设置：合成模式 A", "更正：原图应为 demo-02.tif",
                 "更正：原图应为 demo-03.tif", "更正：原图应为 final-04.tif", "测量：待测"]
        node, _ = self.record("A", notes, True, template)
        self.assertIn("GUV 成像记录", "\n".join(self.wiki.pages[node][1]))
        with patch("review.load_template", side_effect=AssertionError("must use the record-time template")):
            reply = self.run_cmd("复盘本次实验")
        for snippet in ("3个视野", "采集条件", "合成模式 A", "final-04.tif", "本#5", "中间 1 处", "待补项"):
            self.assertIn(snippet, reply)
        self.assertNotIn("· 测量  ", reply)
        self.assertNotIn("99 kg", reply)

    def test_different_explicit_count_denominators_with_source_and_missing_basis(self):
        self.record("A", ["观察：每管计数 12 个囊泡"], True)
        self.record("A", ["A 样品按每个视野计数 4 个囊泡"], True)
        first = self.run_cmd("对比上次实验")
        for fragment in ("计数不能直接比较", "每视野", "每管", "本#1", "上#1"):
            self.assertIn(fragment, first)
        self.record("A", ["观察：数到 20 个，分母待补", "补记：每管 100 µL"], True)
        missing = self.run_cmd("对比上次实验")
        self.assertIn("本次未识别到计数分母", missing)
        self.assertNotIn("本次出现「每管」", missing)
        self.record("A", ["更正：计数分母改为每孔"], True)
        self.assertIn("核对最终采用的分母", self.run_cmd("对比上次实验"))

    def test_draft_without_reference_and_unlabeled_inputs(self):
        self.record("A", ["当时没留下记录，只记得需要再看原图"])
        a = self.run_cmd("对比上次实验")
        self.assertIn("没有可供对照", a)
        self.assertIn("原图", a)


if __name__ == "__main__":
    unittest.main()
