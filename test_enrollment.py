import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from enrollment import PersonalEnrollment, TRANSFER_LABEL, b64url, oaep

APP = "cli_TEST_APP"
FAKE_KEY = "sk-" + "mockKeyForOfflineTestsOnly" * 2


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.store = PersonalEnrollment(self.dir, APP, "https://example.test/offline-form")

    def challenge(self, user):
        return re.search(r"LAB1\.[A-Za-z0-9_-]+", self.store.handle(user, "绑定个人Key")).group()

    def envelope(self, user, key=FAKE_KEY):
        import base64
        code = self.challenge(user).split(".", 1)[1]
        nonce = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))["c"]
        plaintext = json.dumps({"v": 1, "c": nonce, "k": key}).encode()
        return "绑定凭证 " + b64url(self.store._pub.encrypt(plaintext, oaep(TRANSFER_LABEL)))

    def test_member_binding_survives_restart_and_never_saves_plaintext(self):
        envelope = self.envelope("u1")
        restarted = PersonalEnrollment(self.dir, APP, "https://example.test/form")
        self.assertIn("已为", restarted.handle("u1", envelope))
        self.assertEqual(FAKE_KEY, self.store.key_for("u1"))
        self.assertIsNone(self.store.key_for("u2"))
        self.assertIn("无效", self.store.handle("u1", envelope))
        for p in self.dir.iterdir():
            self.assertEqual(0o600, p.stat().st_mode & 0o777)
            self.assertNotIn(FAKE_KEY.encode(), p.read_bytes())

    def test_expiry_sender_and_revocation(self):
        wrong_owner = self.envelope("u1")
        self.challenge("u2")
        self.assertIn("无效", self.store.handle("u2", wrong_owner))
        signed = self.envelope("u2")
        with patch("enrollment.time.time", return_value=time.time() + 601):
            self.assertIn("无效", self.store.handle("u2", signed))
        self.assertIn("已为", self.store.handle("u1", wrong_owner))
        new = self.envelope("u1", FAKE_KEY + "2")
        self.assertEqual(FAKE_KEY, self.store.key_for("u1"))
        self.assertIn("已为", self.store.handle("u1", new))
        self.assertEqual(FAKE_KEY + "2", self.store.key_for("u1"))
        pre_revoke = self.envelope("u1")
        self.assertIn("解绑", self.store.handle("u1", "解绑个人Key"))
        self.assertIsNone(self.store.key_for("u1"))
        self.assertIn("无效", self.store.handle("u1", pre_revoke))
        self.assertIsNone(self.store.handle("u1", "我的实验"))

    def test_only_one_concurrent_redemption_succeeds(self):
        envelope = self.envelope("member")
        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(lambda _: self.store.handle("member", envelope), range(2)))
        self.assertEqual(sum("已为" in response for response in answers), 1)

    def test_beginner_questions_start_personal_binding_without_a_key(self):
        for request in ("怎么绑定", "绑定个人K", "怎么绑定个人 K？", "如何绑定自己的 DS API Key"):
            with self.subTest(request=request):
                answer = self.store.handle("newcomer", request)
                self.assertIn("https://platform.deepseek.com/api_keys", answer)
                self.assertRegex(answer, r"LAB1\.[A-Za-z0-9_-]+$")
        self.assertIsNone(self.store.handle("newcomer", "怎么绑定实验样品编号？"))

    @unittest.skipUnless(shutil.which("node"), "needs Node WebCrypto")
    def test_exact_offline_form_ciphertext_decrypts(self):
        # Execute the shipped HTML code with Node's real Web Crypto and a minimal form DOM.
        script = """
const fs = require('node:fs'), vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {value:'', addEventListener(event, cb){this[event]=cb}});
  return elements.get(id);
}
const dom = {document:{getElementById:el}, crypto:webcrypto, location:{protocol:'file:'},
             TextEncoder, TextDecoder, atob, btoa};
const html = fs.readFileSync(process.argv[1], 'utf8');
vm.runInNewContext(html.match(/<script>([\\s\\S]*?)<\\/script>/)[1], dom);
const form = JSON.parse(fs.readFileSync(0, 'utf8'));
el('challenge').value = form.code; el('key').value = form.key;
(async () => {await el('generate').click(); process.stdout.write(el('result').value);})();
"""
        form_path = self.dir / "public_form.html"
        form_path.write_text(self.store.render_form(), encoding="utf-8")
        p = subprocess.run(["node", "-e", script, str(form_path)], check=True, text=True,
                           input=json.dumps({"code": self.challenge("u1"), "key": FAKE_KEY}), capture_output=True, timeout=6)
        with tempfile.TemporaryDirectory() as other_dir:
            other = PersonalEnrollment(other_dir, APP, "https://example.test/form")
            replacement = re.search(r"LAB1\.[A-Za-z0-9_-]+", other.handle("u1", "绑定个人Key")).group()
            wrong = subprocess.run(["node", "-e", script, str(form_path)], check=True, text=True,
                                   input=json.dumps({"code": replacement, "key": FAKE_KEY}),
                                   capture_output=True, timeout=6)
            self.assertEqual("", wrong.stdout)
        self.assertTrue(p.stdout.startswith("绑定凭证 "))
        self.assertIn("已为", self.store.handle("u1", p.stdout))
        self.assertEqual(self.store.key_for("u1"), FAKE_KEY)


if __name__ == "__main__":
    unittest.main()
