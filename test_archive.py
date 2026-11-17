"""Archive workflow on a private temporary queue with two simulated Feishu members."""
import json
import tempfile
import unittest
from itertools import count
from pathlib import Path
from types import SimpleNamespace

from archive import Archive, ArchiveError, Rejected, Wiki
from state import State

APP, TENANT = "cli_sample_app", "sample_tenant"
SPACE, ROOT = "1234567890123456789", "MemberRootExample"
HOME = "https://example.feishu.cn/wiki/"


class FakeWiki:
    def __init__(self):
        self.documents = {}
        self.create_calls = 0
        self.writes = []
        self.reject = False
        self.lose_create = False
        self.visible = True
        self.block_sharing = False
        self.restricted = set()

    def verify_member_page(self, parent, member_root):
        if parent not in {"ParentSyntheticA", "ParentSyntheticB"} or member_root != ROOT:
            raise ArchiveError("个人目录位置无法核实。")

    def nodes(self, parent):
        return [node for node, _ in self.documents.values() if self.visible and node["parent_node_token"] == parent]

    def create(self, parent, title):
        self.create_calls += 1
        if self.reject:
            raise Rejected("尚未授权")
        doc = f"DocSynthetic{self.create_calls:04d}"
        node = {"node_token": f"WikiSynthetic{self.create_calls:04d}", "obj_token": doc,
                "obj_type": "docx", "node_type": "origin", "parent_node_token": parent,
                "space_id": SPACE, "title": title, "node_creator": APP}
        self.documents[doc] = (node, [])
        if self.lose_create:
            raise TimeoutError("response lost after creating the page")
        return node

    def owns(self, node):
        return node.get("node_creator") == APP

    def limit_sharing(self, node, doc):
        if self.block_sharing:
            raise Rejected("测试目录分享范围无法收紧")
        if self.documents[doc][0]["node_token"] != node:
            raise AssertionError("wiki node and underlying document do not match")
        self.restricted.add(doc)

    def current(self, doc):
        _, lines = self.documents[doc]
        return len(lines) + 1, list(lines)

    def append(self, doc, revision, key, offset, lines):
        if doc not in self.restricted:
            raise AssertionError("content sent before sharing settings were checked")
        current_revision, have = self.current(doc)
        if (revision, offset) != (current_revision, len(have)):
            raise AssertionError("stale document revision or duplicate append")
        self.documents[doc][1].extend(lines)
        self.writes.append((doc, key, offset))


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.state = State(folder / "queue.sqlite")
        self.wiki = FakeWiki()
        self.parents = {"ou_A": "ParentSyntheticA", "ou_B": "ParentSyntheticB"}
        self.routes = folder / "routes.json"
        routes = {"open_id:" + user: {"parent": parent, "name": user} for user, parent in self.parents.items()}
        routes["union_id:shared_A"] = routes["open_id:ou_A"]
        self.routes.write_text(json.dumps({"space_id": SPACE, "app_id": APP, "tenant_key": TENANT, "member_root": ROOT,
                                          "wiki_base_url": HOME, "routes": routes}))
        self.archive = Archive(self.state, self.wiki, self.routes, APP, TENANT)
        self.messages = count(1)

    def row(self, user, text):
        index = next(self.messages)
        return {"user": user, "input": text, "id": f"msg_{index}", "created": 1_800_000_000 + index,
                "archive_parent": self.archive.route(SimpleNamespace(open_id=user))}

    def command(self, user, text):
        return self.archive.command(self.row(user, text))

    def test_missing_routes_disable_archives_but_leave_chat_available(self):
        archive = Archive(self.state, self.wiki, self.routes.with_name("absent.json"), APP, TENANT)
        self.assertIsNone(archive.route(SimpleNamespace(open_id="ou_A")))
        self.assertIsNone(archive.command(self.row("ou_A", "你好")))
        self.assertIn("尚未配置", archive.command(self.row("ou_A", "开始记录")))

    def test_reject_cross_tenant_identity_and_unsafe_host(self):
        config = json.loads(self.routes.read_text())
        for change in ({"tenant_key": "other"}, {"app_id": "other"}, {"space_id": "../"},
                       {"member_root": "/wiki/not-a-node"},
                       {"wiki_base_url": "https://example.feishu.cn.evil.test/wiki/"},
                       {"wiki_base_url": "https://example.feishu.cn/wiki/?redirect=bad"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.routes.write_text(json.dumps({**config, **change}))
                Archive(self.state, self.wiki, self.routes, APP, TENANT)
        self.routes.write_text(json.dumps({**config, "wiki_base_url": "https://test.larksuite.com/wiki"}))
        archive = Archive(self.state, self.wiki, self.routes, APP, TENANT)
        self.assertEqual(archive.wiki_base, "https://test.larksuite.com/wiki/")

    def test_two_member_ids_cannot_claim_same_personal_directory(self):
        config = json.loads(self.routes.read_text())
        config["routes"]["open_id:ou_Another"] = dict(config["routes"]["open_id:ou_A"])
        self.routes.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "冲突"):
            Archive(self.state, self.wiki, self.routes, APP, TENANT)

    def test_moved_parent_is_refused_before_any_write(self):
        from unittest.mock import Mock
        live = Wiki(None)
        live.space_id = SPACE
        live.request = Mock(return_value={"node": {"node_token": "ParentSyntheticA", "space_id": SPACE,
                                                  "node_type": "origin", "parent_node_token": "Elsewhere"}})
        with self.assertRaises(ArchiveError):
            live.verify_member_page("ParentSyntheticA", ROOT)
        live.request.assert_called_once_with("GET", "/open-apis/wiki/v2/spaces/get_node",
                                             query={"token": "ParentSyntheticA"})

    def test_member_routing_order_correction_and_replay(self):
        self.assertFalse(self.archive.note(self.row("ou_A", "普通对话")))
        self.assertIn("尚未", self.command("stranger", "开始记录"))
        self.assertIsNone(self.archive.route(SimpleNamespace(open_id="ou_B", union_id="shared_A")))
        for user in self.parents:
            self.command(user, "开始记录 模拟称量")
        wrong_parent = dict(self.row("ou_A", "不能落到别人的目录"), archive_parent=self.parents["ou_B"])
        self.assertFalse(self.archive.note(wrong_parent))
        self.assertNotIn("已归档到", self.archive.command({**wrong_parent, "input": "记录完成"}))
        first = self.row("ou_A", "A 原值 1 克")
        self.archive.note(first)
        self.archive.note(first)
        self.archive.note(self.row("ou_B", "B 原值 3 克"))
        self.archive.note(self.row("ou_A", "A 更正为 2 克"))
        preview = self.command("ou_A", "查看记录")
        self.assertEqual(preview.count("A 原值 1 克"), 1)
        self.assertLess(preview.index("A 原值"), preview.index("A 更正"))
        self.assertNotIn("B 原值", preview)
        self.assertNotIn("不能落到别人的目录", preview)
        receipts = {user: self.command(user, "记录完成").splitlines()[-1] for user in self.parents}
        self.assertNotEqual(*receipts.values())
        for user, parent in self.parents.items():
            [node] = self.wiki.nodes(parent)
            self.assertEqual(receipts[user], HOME + node["node_token"])
            self.assertIn(receipts[user], self.command(user, "记录完成"))
            self.assertFalse(self.archive.note(self.row(user, "归档后的随口一说")))
            _, lines = self.wiki.current(node["obj_token"])
            if user == "ou_A":
                self.assertEqual([s for s in lines if s.startswith(("A ", "B "))], ["A 原值 1 克", "A 更正为 2 克"])
            else:
                self.assertEqual([s for s in lines if s.startswith(("A ", "B "))], ["B 原值 3 克"])
        self.assertEqual(self.wiki.create_calls, 2)
        self.assertEqual(len(self.wiki.writes), 2)

    def test_lost_create_response_survives_restart_and_reconciliation(self):
        self.command("ou_A", "开始记录 演练")
        self.archive.note(self.row("ou_A", "模拟结果 A"))
        self.wiki.lose_create = True
        self.assertIn("草稿已保留", self.command("ou_A", "记录完成"))
        self.wiki.visible = False
        self.archive = Archive(self.state, self.wiki, self.routes, APP, TENANT)
        self.assertNotIn("已归档到", self.command("ou_A", "记录完成"))
        self.assertEqual(self.wiki.create_calls, 1)
        self.wiki.visible = True
        receipt = self.command("ou_A", "记录完成")
        self.assertIn("已归档到", receipt)
        self.assertIn(HOME, receipt)
        self.assertEqual(self.wiki.create_calls, 1)
        self.assertEqual(len(self.wiki.writes), 1)

    def test_rejected_create_retains_editable_draft(self):
        self.command("ou_A", "开始记录")
        self.archive.note(self.row("ou_A", "模拟初次观察"))
        self.wiki.reject = True
        self.assertIn("草稿已保留", self.command("ou_A", "记录完成"))
        self.assertFalse(self.wiki.documents)
        self.assertIn("模拟初次观察", self.command("ou_A", "查看记录"))
        self.assertTrue(self.archive.note(self.row("ou_A", "取得授权前的模拟补记")))
        self.wiki.reject = False
        self.assertIn("2 条口述", self.command("ou_A", "记录完成"))

    def test_unverified_sharing_keeps_title_and_body_out_of_empty_page(self):
        self.command("ou_A", "开始记录 模拟课题名")
        self.archive.note(self.row("ou_A", "模拟观察结果"))
        self.wiki.block_sharing = True
        self.assertIn("草稿已保留", self.command("ou_A", "记录完成"))
        [(node, body)] = self.wiki.documents.values()
        self.assertNotIn("模拟课题名", node["title"])
        self.assertEqual(body, [])
        self.assertFalse(self.wiki.writes)
        self.assertIn("模拟观察结果", self.command("ou_A", "查看记录"))
        self.wiki.block_sharing = False
        self.assertIn(HOME + node["node_token"], self.command("ou_A", "记录完成"))
        self.assertEqual(self.wiki.create_calls, 1)
        self.assertIn("模拟观察结果", body)

    def test_fixed_fields_copy_only_user_labels_and_preserve_original_units(self):
        self.command("ou_A", "开始记录 模拟称量")
        raw = ("样品：SIM-01；测量：10.0 g，计划：再称 0.020 kg\n"
               "目的: 核对天平显示；文件名：sample-01.tif\n未标注的 91 mg 仍只作为口述。")
        self.archive.note(self.row("ou_A", raw))
        self.archive.note(self.row("ou_A", "更正：第一次读数应为 9.90 g"))
        self.assertIn("已归档到", self.command("ou_A", "记录完成"))
        [(_, lines)] = self.wiki.documents.values()
        cutoff = next(i for i, line in enumerate(lines) if line.startswith("固定字段摘要"))
        transcript, summary = lines[:cutoff], lines[cutoff:]
        self.assertTrue(all(line in transcript for line in raw.splitlines()))
        self.assertIn("更正：第一次读数应为 9.90 g", transcript)
        for line in ("记录成员（飞书账号）：ou_A", "操作者：待补", "实验日期：待补", "实验名称：模拟称量",
                     "目的: 核对天平显示", "样品：SIM-01", "计划：再称 0.020 kg", "实际操作：待补",
                     "测量：10.0 g", "文件名：sample-01.tif", "更正：第一次读数应为 9.90 g"):
            self.assertIn(line, summary)
        self.assertNotIn("91 mg", "\n".join(summary))

    def test_lost_create_response_cannot_recover_a_foreign_document(self):
        self.command("ou_B", "开始记录 模拟")
        self.archive.note(self.row("ou_B", "模拟结果 B"))
        self.wiki.lose_create = True
        self.assertIn("草稿已保留", self.command("ou_B", "记录完成"))
        [(node, body)] = self.wiki.documents.values()
        node["node_creator"] = "other_synthetic_app"
        self.assertIn("创建身份无法确认", self.command("ou_B", "记录完成"))
        self.assertFalse(body)
        self.assertFalse(self.wiki.writes)
        self.assertFalse(self.wiki.restricted)
        self.assertEqual(self.wiki.create_calls, 1)
        self.assertIn("模拟结果 B", self.command("ou_B", "查看记录"))
