"""Offline service regressions. No API calls, credentials, or Feishu messages."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from bot import Bot, inbound
from lab import Courses
from state import State


def event(text, ident="om_1", member="ou_A", tenant="t1", source="user", kind="p2p", app="cli_X"):
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id=member), sender_type=source)
    message = SimpleNamespace(message_type="text", message_id=ident, chat_type=kind,
                              content=json.dumps({"text": text}))
    return SimpleNamespace(header=SimpleNamespace(app_id=app, tenant_key=tenant),
                           event=SimpleNamespace(sender=sender, message=message))


class OfflineBotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.state = State(self.path / "queue")
        self.courses = Courses()
        self.sent, self.calls = {}, []

        def generate(key, model, messages):
            self.calls.append((key, messages))
            return "回读：" + messages[-1]["content"]

        self.bot = Bot(self.state, self.courses, None, "dummy", "mock", self.sent.__setitem__, generate)

    def push(self, *args, **kwargs):
        item = inbound(event(*args, **kwargs), "cli_X", "t1")
        if item:
            self.state.enqueue(*item)

    def drain(self):
        for _ in range(100):
            if not self.bot.step():
                return
        self.fail("queue did not drain")

    def test_auth_key_and_dm_boundaries(self):
        for override in ({"app": "cli_Y"}, {"tenant": "other"}, {"kind": "group"}, {"source": "app"}):
            self.push("ignore", **override)
        self.push("我的密钥是sk-" + "x" * 30)
        self.drain()
        self.assertEqual(len(self.calls), 0)
        self.assertIn("密钥", self.sent["om_1"])
        self.assertNotIn(b"x" * 30, (self.path / "queue").read_bytes())

    def test_dedup_personal_routing_reset_and_same_member_order(self):
        self.bot.personal = SimpleNamespace(key_for={"ou_A": "one", "ou_B": "two"}.get)
        self.bot.shared_key = "owner"
        self.push("样品 a", "om_1")
        self.push("重复", "om_1")
        self.push("样品 b", "om_2", "ou_B")
        self.push("继续补记", "om_3")
        self.push("清除上下文", "om_4")
        self.push("重新开始", "om_5")
        self.push("未绑定", "om_6", "ou_C")
        self.drain()
        observed = {messages[-1]["content"]: (key, messages[1:-1]) for key, messages in self.calls}
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(observed["样品 b"], ("two", []))
        self.assertEqual(observed["继续补记"][1][0]["content"], "样品 a")
        self.assertEqual(observed["重新开始"], ("one", []))
        self.assertIn("尚未绑定", self.sent["om_6"])

    def test_static_without_keys_and_course_material_stays_local(self):
        self.bot.shared_key = None
        self.push("安全培训 演示灯", "om_1")
        self.push("GUV模板", "om_2")
        self.push("我的实验", "om_3")
        self.drain()
        self.assertIn("模拟设备编号", self.sent["om_1"])
        self.assertIn("原始图像", self.sent["om_2"])
        self.assertIn("尚未配置", self.sent["om_3"])
        self.assertFalse(self.calls)
        self.bot.shared_key = "mock"
        self.push("准备开启演示灯，帮我记", "om_4")
        self.push("继续记录", "om_5")
        self.drain()
        self.assertIn("模拟设备编号", self.sent["om_4"])
        self.assertNotIn("模拟设备编号", str(self.calls))
        self.assertNotIn("关键词匹配", str(self.calls))

    def test_restart_after_inference_reuses_reply_and_interrupted_work_is_not_rebilled(self):
        self.push("第一条", "om_1")
        self.assertTrue(self.bot.step())
        self.state = self.bot.store = State(self.path / "queue")
        self.drain()
        self.push("试试", "om_2")
        self.assertEqual(self.state.take()["stage"], "queue")
        self.state = self.bot.store = State(self.path / "queue")
        self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("上一条处理被中断", self.sent["om_2"])

    def test_parallel_workers_reserve_each_member_in_fifo_order(self):
        self.push("甲第一条", "om_1", "ou_A")
        self.push("甲第二条", "om_2", "ou_A")
        self.push("乙第一条", "om_3", "ou_B")
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(lambda _: self.state.take(), range(3)))
        self.assertEqual({row["id"] for row in rows if row}, {"om_1", "om_3"})
        self.state.ready("om_1", "甲一回复")
        self.assertEqual(self.state.take()["id"], "om_1")
        self.state.delivered("om_1")
        self.assertEqual(self.state.take()["id"], "om_2")

    def test_enrollment_intercepts_model_history_and_removes_envelope(self):
        class Personal:
            def handle(self, user, text):
                return "同一人处理" if user == "ou_A" else "错误"
            def key_for(self, user):
                raise AssertionError("personal key lookup must be skipped for enrollment")
        self.bot.personal = Personal()
        self.push("绑定个人Key", "om_1")
        self.push("绑定凭证 " + "A"*250, "om_2")
        self.drain()
        self.assertIn("同一人处理", self.sent["om_1"])
        self.assertIn("同一人处理", self.sent["om_2"])
        self.assertFalse(self.calls)
        self.assertNotIn(b"A"*200, (self.path / "queue").read_bytes())
        self.assertFalse(self.state.history("ou_A"))


if __name__ == "__main__":
    unittest.main()
