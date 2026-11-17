"""Start a Feishu websocket bot with a team or member-supplied DeepSeek API key."""
import fcntl
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from enrollment import PersonalEnrollment, is_enrollment_message
from guv_capture import extract_fields, missing_question
from lab import Courses, DEFAULT_RULES, EMERGENCY, SECRET, SECRET_NOTICE, SYSTEM
from review import review_command
from state import State

LOG = logging.getLogger("lab_assistant")
MAX_INPUT = 6000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def complete(key, model, messages):
    data = {"model": model, "messages": messages, "stream": False,
            "thinking": {"type": "disabled"}, "max_tokens": 1800}
    req = Request("https://api.deepseek.com/chat/completions",
                  data=json.dumps(data).encode(), method="POST",
                  headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with build_opener(NoRedirect).open(req, timeout=60) as response:
        result = json.load(response)["choices"][0]
    content = result["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty completion")
    return content.strip() + ("\n（回复已达到长度上限；可继续分段整理。）" if result.get("finish_reason") == "length" else "")


def inbound(event, app, tenant):
    header, data = event.header, event.event
    if not header or header.app_id != app or header.tenant_key != tenant or not data:
        return None
    sender, msg = data.sender, data.message
    if not sender or sender.sender_type != "user" or not msg or msg.chat_type != "p2p":
        return None
    if getattr(sender, "tenant_key", None) not in (None, "", tenant):
        return None
    user = sender.sender_id.open_id if sender.sender_id else None
    if not user or not msg.message_id:
        return None
    if msg.message_type != "text":
        return msg.message_id, user, None, "目前支持单聊文字，可用飞书语音转文字后再发送。"
    try:
        text = json.loads(msg.content)["text"].strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not text:
        return None
    if SECRET.search(text):
        return msg.message_id, user, None, SECRET_NOTICE
    if len(text) > MAX_INPUT:
        return msg.message_id, user, None, f"此条超过 {MAX_INPUT} 字，请按样品或时间分段发送。"
    return msg.message_id, user, text, None


class Bot:
    def __init__(self, store, courses, personal, shared_key, model, reply, ask=complete, notify_safety=None, archive=None):
        self.store, self.courses = store, courses
        self.personal, self.shared_key = personal, shared_key
        self.model, self.reply, self.ask = model, reply, ask
        self.archive = archive
        self.notify_safety = notify_safety

    def prepare(self, row):
        ident, user, text = row["id"], row["user"], row["input"]
        if row["notice"]:
            return row["notice"], None
        if is_enrollment_message(text):
            if self.personal is None:
                return "本机器人使用组内共用计费；当前模式不接受个人密钥登记。", None
            return self.personal.handle(user, text) or "绑定凭证格式无效，请发送「绑定个人Key」重新开始。", None
        if re.sub(r"\s+", "", text).lower().strip("。？?!！") in {"清除上下文", "/reset"}:
            self.store.clear(user, ident)
        if EMERGENCY.search(text):
            return self.courses.local_answer(text, user, personal=self.personal is not None), None
        tip = self.courses.before_step(text)
        prefix = f"组内安全提醒：{tip}\n\n" if tip else ""
        result = review_command(self.archive, row, wiki_base=getattr(self.archive, "wiki_base", None),
                                space_id=getattr(self.archive, "space_id", None))
        if result is not None:
            return result, None
        if self.archive is not None:
            command = self.archive.command(row)
            if command is not None:
                return prefix + command, None
        answer = self.courses.local_answer(text, user, personal=self.personal is not None)
        if answer:
            return answer, None
        recorded = self.archive is not None and self.archive.note(row)
        receipt = "已记入本次草稿。发「记录完成」归档。\n" if recorded else ""
        current = self.archive.active(user) if recorded else None
        if current is not None and current["capture_template"]:
            template = json.loads(current["capture_template"])
            notes = self.archive.entries(current["id"])
            fields = extract_fields(template, [entry["content"] for entry in notes])
            matched = [field["label"] for field in template["fields"]
                       if any(n == len(notes) for n, _ in fields[field["id"]])]
            detail = "本条摘录归入「" + "、".join(matched) + "」。" if matched else "本条已按原话保留。"
            question = missing_question(template, fields)
            return prefix + receipt + detail + ("\n" + question if question else ""), None
        key = self.personal.key_for(user) if self.personal is not None else self.shared_key
        if not key:
            if self.personal is None:
                return prefix + receipt + "组内共用 DeepSeek Key 尚未配置。可先用「通用模板」「GUV模板」或「安全培训」。", None
            return prefix + receipt + "个人计费尚未绑定。请发送「绑定个人Key」，按指引在本地页面输入自己的 DeepSeek API Key。绑定前可直接查课件和空白模板。", None
        if prefix and self.notify_safety:
            try:
                self.notify_safety(ident, prefix.rstrip())
            except Exception:
                LOG.warning("安全提醒先行回信未确认，将随记录回复一并送出")
            else:
                prefix = ""
        history = self.store.history(user)
        if current is not None:
            recent = self.archive.entries(current["id"])[-6:-1]
            history = [{"role": "user", "content": "我为本次记录填写的实验名称：" + current["title"]}]
            history.extend({"role": "user", "content": entry["content"]} for entry in recent)
        system = SYSTEM
        if tip:
            system += ("\n本步已核对的组内提醒会由服务直接展示。仅整理成员的记录或演练，保留计划与实际的区别。"
                       "不要另拟笼统安全须知、操作参数或急救流程，也不要把提醒当作已执行的操作。")
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]
        try:
            model_answer = self.ask(key, self.model, messages)
        except HTTPError as exc:
            problem = {401: "API Key 认证失败", 402: "DeepSeek 余额不足", 429: "DeepSeek 当前限流"}.get(exc.code, "DeepSeek 服务暂不可用")
            return prefix + receipt + f"{problem}。请稍后重试或联系 Key 持有人。可先发「安全培训」或「通用模板」。", None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return prefix + receipt + "未收到完整的 DeepSeek 回复，可稍后重试。可先发「安全培训」或「通用模板」。", None
        return prefix + receipt + model_answer, None if recorded else model_answer

    def step(self):
        row = self.store.take()
        if not row:
            return False
        if row["stage"] == "queue":
            try:
                answer, history = self.prepare(row)
            except Exception:
                LOG.error("处理消息失败，请检查本地配置")
                answer, history = "本次处理失败，请联系管理员检查配置。可先发「安全培训」或「通用模板」。", None
            self.store.ready(row["id"], answer, history)
        else:
            try:
                self.reply(row["id"], row["answer"])
            except Exception:
                LOG.warning("向飞书回复失败，将按原消息编号重试")
                self.store.retry(row)
            else:
                self.store.delivered(row["id"])
        return True

    def run(self):
        while True:
            try:
                if not self.step():
                    time.sleep(0.4)
            except Exception:
                LOG.error("本地消息队列暂时不可用")
                time.sleep(2)


def serve():
    import lark_oapi as lark
    from archive import Archive, Wiki
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    env = os.environ
    app, secret, tenant = (env.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TENANT_KEY"))
    if not all((app, secret, tenant)):
        raise ValueError("需要 FEISHU_APP_ID、FEISHU_APP_SECRET 和 FEISHU_TENANT_KEY")
    mode = env.get("DS_KEY_MODE", "personal")
    if mode not in {"personal", "shared"}:
        raise ValueError("DS_KEY_MODE 须为 personal 或 shared")
    courses = Courses(Path(env.get("LAB_SAFETY_RULES", DEFAULT_RULES)).expanduser())
    os.umask(0o077)
    folder = Path(env.get("LAB_STATE_DIR", "~/.local/state/feishu-lab-assistant")).expanduser()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("状态目录须为当前服务账户私有目录（chmod 700）")
    lock = (folder / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = State(folder / "messages.sqlite3")

    client = lark.Client.builder().app_id(app).app_secret(secret).timeout(20).log_level(lark.LogLevel.ERROR).build()

    route_path = Path(env.get("LAB_ARCHIVE_ROUTES", str(folder / "archive_routes.json"))).expanduser()
    archive = Archive(store, Wiki(client), route_path, app, tenant)

    def reply(ident, answer, *, safety=False):
        delivery_id = app + "/" + ident + ("/safety" if safety else "")
        body = (ReplyMessageRequestBody.builder().msg_type("text")
                .content(json.dumps({"text": answer}, ensure_ascii=False))
                .uuid(str(uuid.uuid5(uuid.NAMESPACE_URL, delivery_id))).build())
        req = ReplyMessageRequest.builder().message_id(ident).request_body(body).build()
        if not client.im.v1.message.reply(req).success():
            raise RuntimeError("Feishu reply failed")

    def receive(event):
        item = inbound(event, app, tenant)
        if item:
            parent = archive.route(event.event.sender.sender_id)
            store.enqueue(*item, archive_parent=parent)

    def cleaner():
        while True:
            time.sleep(60)
            store.prune()

    personal = PersonalEnrollment(folder, app, env.get("LAB_BIND_URL", "")) if mode == "personal" else None
    shared_key = env.get("DEEPSEEK_API_KEY") if mode == "shared" else None
    bot = Bot(store, courses, personal, shared_key, env.get("DEEPSEEK_MODEL", "deepseek-flash"), reply,
              notify_safety=lambda ident, tip: reply(ident, tip, safety=True), archive=archive)
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).build()
    for _ in range(4):
        threading.Thread(target=bot.run, daemon=True).start()
    threading.Thread(target=cleaner, daemon=True).start()
    lark.ws.Client(app, secret, event_handler=handler, log_level=lark.LogLevel.ERROR).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        serve()
    except (ValueError, FileNotFoundError, BlockingIOError) as error:
        raise SystemExit(str(error))
