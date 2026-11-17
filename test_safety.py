"""Simulated safety rule tests using invented equipment labels."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from bot import Bot
from lab import Courses, URGENT


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.courses = Courses()

    def test_upcoming_action_and_retrospective(self):
        for text in ("刚做完旧项目，马上开演示灯", "模拟演练，下一步准备开演示灯"):
            self.assertIn("演示设备标签", self.courses.before_step(text))
        for text in ("昨天准备开演示灯", "接下来分析演示灯图像", "模拟记录，已经完成演示灯标签检查",
                     "准备做实验", "准备去其他设备", "接下来先不开演示灯"):
            self.assertEqual("", self.courses.before_step(text), text)

    def test_local_rule_file_and_emergency_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rules.json"
            path.write_text(json.dumps({"rules": [{"title": "纸卡演练", "keywords": ["纸卡"],
                            "reminder": "核对纸卡上的模拟编号。", "source": "本地模拟例题"}]}))
            courses = Courses(path)
            self.assertIn("纸卡上的模拟编号", courses.before_step("我去看纸卡"))
            self.assertIn("本地模拟例题", courses.local_answer("安全培训 纸卡", "user", True))
            self.assertEqual(URGENT, courses.local_answer("我去看纸卡，现在着火了", "user", True))
            self.assertEqual("", self.courses.before_step("我去看纸卡"))

    def test_local_reminder_without_deepseek_and_remote_failure(self):
        calls = []

        class FakeState:
            def history(self, _user):
                return []

        def ask(_key, _model, messages):
            self.assertNotIn("仓库内虚构", str(messages))
            calls.append("model")
            raise HTTPError("unused", 402, "", {}, None)

        def notify(_ident, tip):
            self.assertIn("演示设备标签", tip)
            calls.append("notice")

        bot = Bot(FakeState(), self.courses, None, None, "test", None, ask, notify_safety=notify)
        row = {"id": "test", "user": "user", "input": "准备开演示灯", "notice": None}
        self.assertIn("演示设备标签", bot.prepare(row)[0])
        self.assertEqual([], calls)
        bot.shared_key = "mock"
        self.assertIn("余额不足", bot.prepare(row)[0])
        self.assertEqual(["notice", "model"], calls)


if __name__ == "__main__":
    unittest.main()
