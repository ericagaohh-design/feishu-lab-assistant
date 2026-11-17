"""Self-service binding for one configured Feishu app. The form works entirely offline."""
import base64
import binascii
import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CHALLENGE_SECONDS = 600
FORM_NAME = "bind-key.html"
KEYS_URL = "https://platform.deepseek.com/api_keys"
TRANSFER_LABEL = b"Lab-Feishu-Enrollment-v1"
KEY = re.compile(r"sk-[A-Za-z0-9_-]{16,180}")
CREDENTIAL = re.compile(r"绑定凭证\s+([A-Za-z0-9_-]{512})", re.IGNORECASE)
COMMAND = re.compile(r"^/?(绑定个人\s*key|解绑个人\s*key|绑定凭证)", re.IGNORECASE)
BIND_REQUEST = re.compile(
    r"/?(?:怎么|如何)?绑定(?:个人|自己的|我的)?(?:deepseek|ds)?(?:api)?(?:key|k|密钥)?[？?。！!]?",
    re.IGNORECASE,
)


def is_bind_request(text):
    return BIND_REQUEST.fullmatch("".join(text.split())) is not None


def is_enrollment_message(text):
    return isinstance(text, str) and (is_bind_request(text) or COMMAND.match(text.strip()) is not None)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def oaep(label):
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=label)


def private_fd(path):
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    mode = os.fstat(fd)
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        os.close(fd)
        raise PermissionError("请核对绑定凭据文件属主和 0600 权限。")
    return fd


class PersonalEnrollment:
    def __init__(self, state_dir, app_id, form_url=""):
        self.folder = Path(state_dir).expanduser()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("绑定凭据目录必须由服务用户独占，权限应为 0700。")
        self.app = app_id
        self.form_url = form_url
        self.path = self.folder / "enrollment.sqlite3"
        os.close(private_fd(self.path))
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS challenges (
                app TEXT NOT NULL, sender TEXT NOT NULL, nonce TEXT NOT NULL,
                expires INTEGER NOT NULL, PRIMARY KEY(app,sender))""")
            db.execute("""CREATE TABLE IF NOT EXISTS members (
                app TEXT NOT NULL, sender TEXT NOT NULL, sealed BLOB NOT NULL,
                PRIMARY KEY(app,sender))""")
        self._priv = self._load_private_key()
        self._pub = self._priv.public_key()
        n = self._pub.public_numbers().n
        self._n = b64url(n.to_bytes(384, "big"))

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=8)) as db:
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    def _load_private_key(self):
        path = self.folder / "enrollment-private.pem"
        with os.fdopen(private_fd(self.folder / "enrollment.lock"), "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.is_symlink():
                raise PermissionError("绑定密钥路径必须是当前服务用户的普通文件。")
            if path.exists():
                with os.fdopen(private_fd(path), "rb") as existing:
                    key = serialization.load_pem_private_key(existing.read(), password=None)
                if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 3072 or key.public_key().public_numbers().e != 65537:
                    raise ValueError("绑定服务私钥格式不匹配。")
                return key
            with self.db() as db:
                if db.execute("SELECT EXISTS(SELECT 1 FROM members) OR EXISTS(SELECT 1 FROM challenges)").fetchone()[0]:
                    raise ValueError("绑定服务缺少原私钥，请从同一实例的备份恢复。")
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            with tempfile.NamedTemporaryFile(dir=self.folder, delete=False) as output:
                tmp = Path(output.name)
                try:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            return key

    def render_form(self):
        # Distribute this instance-specific public form via a trusted channel.
        # Only the RSA public modulus and the app ID appear in the rendered HTML.
        if not re.fullmatch(r"cli_[A-Za-z0-9_]+", self.app):
            raise ValueError("Invalid FEISHU_APP_ID.")
        path = Path(__file__).with_name("bind-key.template.html")
        return (path.read_text(encoding="utf-8").replace("__APP_ID__", self.app)
                .replace("__PUBLIC_MODULUS__", self._n))

    def _storage_label(self, sender):
        return ("lab-key-storage-v1|" + self.app + "|" + sender).encode("utf-8")

    def key_for(self, sender):
        with self.db() as db:
            row = db.execute("SELECT sealed FROM members WHERE app=? AND sender=?", (self.app, sender)).fetchone()
        return self._priv.decrypt(row[0], oaep(self._storage_label(sender))).decode("ascii") if row else None

    def _start(self, sender):
        if not self.form_url:
            return "个人 Key 绑定文件尚未发布。请稍后在本单聊发送“绑定个人Key”。"
        nonce = secrets.token_urlsafe(24)
        now = int(time.time())
        expires = now + CHALLENGE_SECONDS
        with self.db() as db:
            db.execute("DELETE FROM challenges WHERE expires<=?", (now,))
            db.execute("INSERT OR REPLACE INTO challenges(app,sender,nonce,expires) VALUES (?,?,?,?)",
                       (self.app, sender, nonce, expires))
        bundle = {"v": 1, "a": self.app, "n": self._n, "e": "AQAB", "c": nonce, "x": expires}
        code = "LAB1." + b64url(json.dumps(bundle, separators=(",", ":")).encode("ascii"))
        return ("绑定后，你用助手整理实验记录时，费用记在你自己的 DeepSeek 账号。查课件和拿模板可直接使用。\n\n"
                f"1. 准备你的个人 Key。它是 DeepSeek 发给你的一串以 sk- 开头的密钥。"
                f"已有 Key 可跳到下一步；还没有的话，请登录自己的账号，创建并复制一个 API Key。\n{KEYS_URL}\n\n"
                f"2. 在电脑上下载《{FORM_NAME}》，双击用浏览器打开。\n{self.form_url}\n"
                "第一栏粘贴本条消息最下面的绑定码，第二栏粘贴你的 sk- 密钥。原始密钥只填在这个密码框里。\n\n"
                "3. 点“生成加密凭证”，再点“复制凭证”，把复制的整段“绑定凭证 …”发回这个飞书单聊。"
                "看到已保存的回复就完成了。\n\n"
                f"你的绑定码（十分钟有效，过期可重新发送“绑定个人Key”）\n{code}")

    def _bind(self, sender, text):
        match = CREDENTIAL.fullmatch(text)
        if not match:
            return "凭证格式不完整。请从本地绑定文件复制生成的整段“绑定凭证 …”。"
        invalid = "凭证无效、已使用或已过期。请在本单聊重新发送“绑定个人Key”。"
        # Check the sender before doing expensive cryptography, then atomically consume.
        with self.db() as db:
            pending = db.execute("SELECT nonce,expires FROM challenges WHERE app=? AND sender=?",
                                 (self.app, sender)).fetchone()
        if not pending or pending[1] <= time.time():
            return invalid
        try:
            cipher = base64.urlsafe_b64decode(match[1])
            payload = json.loads(self._priv.decrypt(cipher, oaep(TRANSFER_LABEL)))
            if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 1:
                return invalid
            key, nonce = payload.get("k"), payload.get("c")
            if not isinstance(key, str) or not KEY.fullmatch(key) or nonce != pending[0]:
                return invalid
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            return invalid
        sealed = self._pub.encrypt(key.encode("ascii"), oaep(self._storage_label(sender)))
        with self.db() as db:
            consumed = db.execute("DELETE FROM challenges WHERE app=? AND sender=? AND nonce=? AND expires>?",
                                  (self.app, sender, nonce, time.time())).rowcount
            if not consumed:
                return invalid
            db.execute("INSERT OR REPLACE INTO members(app,sender,sealed) VALUES (?,?,?)", (self.app, sender, sealed))
        return ("已为你的飞书账号保存个人 DeepSeek Key。下次模型对话会使用它，DeepSeek 届时验证有效性并按你的账号计费。"
                "你可以说“按通用模板带我记实验”，或发送“解绑个人Key”解除绑定。")

    def handle(self, sender, text):
        if not is_enrollment_message(text):
            return None
        text = text.strip().removeprefix("/")
        if is_bind_request(text):
            return self._start(sender)
        if re.fullmatch(r"解绑个人\s*key", text, re.IGNORECASE):
            with self.db() as db:
                db.execute("DELETE FROM challenges WHERE app=? AND sender=?", (self.app, sender))
                db.execute("DELETE FROM members WHERE app=? AND sender=?", (self.app, sender))
            return "你的个人 Key 已解绑，未使用的绑定码已失效。要彻底停用这个 Key，请到 DeepSeek 开放平台删除它。"
        if text.startswith("绑定凭证"):
            return self._bind(sender, text)
        return "请单独发送“绑定个人Key”或“解绑个人Key”。原始 Key 仅在本地绑定文件的密码栏填写。"

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export this bot's pinned offline key-binding form.")
    parser.add_argument("action", choices=["export-form"])
    parser.add_argument("--output", required=True, type=Path, help="New local HTML file to distribute")
    args = parser.parse_args()
    app = os.environ.get("FEISHU_APP_ID", "")
    if not re.fullmatch(r"cli_[A-Za-z0-9_]+", app):
        parser.error("Set FEISHU_APP_ID to the public identifier for your own app.")
    state = Path(os.environ.get("LAB_STATE_DIR", "~/.local/state/feishu-lab-assistant")).expanduser()
    if args.output.resolve().is_relative_to(state.resolve()):
        parser.error("Store the public form outside the private state directory.")
    instance = PersonalEnrollment(state, app)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(instance.render_form())
    print("已导出本实例的离线绑定页面。将文件分发给成员，使用 LAB_BIND_URL 配置下载链接。")
