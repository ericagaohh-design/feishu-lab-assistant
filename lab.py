"""Match locally maintained safety rules and guide typed or dictated lab records."""
import json
import re
from pathlib import Path

DEFAULT_RULES = Path(__file__).resolve().parent / "examples" / "safety_rules.json"
HELP = (
    "发「通用模板」或「GUV模板」取空白记录。发「安全培训」列出本实例的安全提醒，再按主题查阅。"
    "\n发「开始记录 实验名称」开启草稿，发「记录完成」提交到管理员为本人配置的目录。"
    "\n发「我的编号」查询本人在此应用的 open_id。模型整理会将你的口述和近期单聊上下文发给 DeepSeek。"
    "发「清除上下文」可清除服务中此前的模型对话。"
)
SYSTEM = """你是实验记录助理，使用中文。依据当前用户口述整理可复制的对话草稿。
区分计划、实际操作、观察、计算和解释，保留原话、单位、样品编号和文件名。缺项写待补，
每轮最多追问一个影响理解的事项。更正时保留原记录和更正。演练标注模拟。
通用记录按目的、日期、操作者、样品和对照、计划、实际、结果、文件、待补项引导；
GUV 记录再区分各相、组分、批号、计划量与实际量、观察条件。不得编造参数或结果。
你只做记录和一般性的安全引导。需要设备或试剂特定信息时请对方查组内课件、现行 SOP、
产品 SDS 或现场负责人。服务另在本地检索课件，不要虚构课件、链接、页码或紧急电话。
归档由服务处理。成员发「开始记录 实验名称」后口述，发「记录完成」归档。你只整理草稿。已给出的实验名称不再追问，缺项可留待补，不作为归档前提。保存回执以服务的飞书回读为准。培训和签到不自动归档。
涉及危险生物、武器或危险事故的请求，只提供安全、求助和记录层面的帮助。"""

TEMPLATES = {
    "通用": "实验名称与编号：\n日期与操作者：\n实验目的：\n样品编号、分组及对照：\n计划：\n实际操作和测量（时间、样品、参数与单位）：\n观察、偏差与解释：\n原始文件、样品对应关系：\n收尾、待补项与更正：",
    "GUV": "实验编号、日期与操作者：\n目的、样品编号及组别：\n各相（按实际填写）、组分、批号、用量及原单位：\n计划与实际：\n设备、观察条件及时间：\n逐样品观察、读数和偏差：\n计算与解释：\n原始图像、样品及视野对应关系：\n收尾、待补项与更正：",
}
SECRET = re.compile(r"(?i)(?<![a-z0-9_-])sk[-_][A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._+-]{12,}")
SECRET_NOTICE = "这条消息看起来含有密钥，已跳过模型和本地正文保存。如是真实 Key，请从飞书撤回并在服务商后台轮换；个人 Key 请通过「绑定个人Key」的本地加密页面配置。"
URGENT = "如果现场出现人员受伤、泄漏、起火或报警，请先远离危险，通知现场人员，按单位应急预案联系援助，现场安全后再补记。"
EMERGENCY = re.compile("我.{0,5}(被溅到|受伤了|烫伤了|眼睛进了)|现在.{0,6}(起火|泄漏|报警)|着火了|有人.{0,3}(昏倒|受伤)")
STEP = re.compile(r"接下来|下一步|马上|待会儿?|等下|即将|打算|计划|切换(?:到)?|收尾|"
                  r"准备(?!好了?|就绪|完毕|工作|了)|(?:我(?:现在|今天)?要|今天要|我去|现在要|现在去|现在开始|我正要)|"
                  r"(?:我)?正在|^(?:开始|我先)|(?:我现在|现在|我在)(?=给|用|开|取|上|配|启动|分装|转移|清理|处理|倾倒)")
RETROSPECTIVE = re.compile(r"复盘|回顾|回看|补录|文献|模拟|已完成|做完了|已经|刚才|刚刚|昨天|上次")
SIM_LABEL = re.compile(r"^[【（(\s]*(?:模拟演练|模拟记录|模拟场景|模拟(?=[】）)\s：:，,。]))[】）)\s：:，,。]*")
DESK_WORK = re.compile(r"(?:分析|处理|整理|汇总|检查|查看|复核|导出|导入|标注).{0,16}(?:数据|图像|图片|照片|文件|旧记录|结果)|"
                      r"离线.{0,12}分析|数据分析|读(?:论文|文献)|查(?:文献|资料)|写(?:报告|综述)")
NON_ACTION = re.compile(r"^(?:先)?(?:不|别|暂停|取消|避免|跳过)|刚(?:用|做|开|关)完|完毕|已经|完成了|做完了|用完了|结束了|时.{0,12}了|好了$")


def upcoming_step(text):
    # Match the intended action only. Past results can precede it in the same dictation.
    text = re.sub(r"^(?:开始实验记录|开始记录|新建实验记录)[\s：:，,]*", "", text.strip())
    if re.match(r"^[【\s]*(?:复盘|回顾|补录|整理旧记录|(?:桌面|软件|数值)模拟)", text):
        return ""
    text = SIM_LABEL.sub("", text, count=1)
    for clause in re.split(r"[，,。；;！？!?\n]", text):
        cue = STEP.search(clause)
        if not cue:
            continue
        before = clause[:cue.start()]
        follows_finished = re.search(r"(?:刚.{0,8}完|结束).{0,16}$", before)
        if RETROSPECTIVE.search(before) and not (follows_finished and cue[0] in {"马上", "接下来", "下一步"}):
            continue
        if re.search(r"(?:不再?|原本|本来)$", before):
            continue
        action = clause[cue.end():].strip()
        if action and not (RETROSPECTIVE.search(action) or NON_ACTION.search(action) or DESK_WORK.search(action)):
            return action
    return ""



class Courses:
    """Read team-authored reminders from a JSON file. No network requests."""

    def __init__(self, path=DEFAULT_RULES):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.rules = payload["rules"]
        if not isinstance(self.rules, list) or not all(
            isinstance(rule, dict) and all(isinstance(rule.get(field), str) and rule[field].strip()
                                           for field in ("title", "reminder", "source"))
            and isinstance(rule.get("keywords"), list) and rule["keywords"]
            and all(isinstance(word, str) and word.strip() for word in rule["keywords"])
            for rule in self.rules
        ):
            raise ValueError("规则需要 title、keywords（非空列表）、reminder 和 source。")

    def match(self, text):
        query = text.casefold()
        for rule in self.rules:
            if any(label.strip().casefold() in query for label in (rule["title"], *rule["keywords"])):
                return f"《{rule['title']}》\n{rule['reminder']}\n出处：{rule['source']}"
        return ""

    def before_step(self, text):
        action = upcoming_step(text)
        return self.match(action) if action else ""

    def local_answer(self, text, user, personal):
        query = re.sub(r"\s+", "", text).lower().strip("。？?!！")
        if EMERGENCY.search(text):
            return URGENT
        if query in {"帮助", "/help", "你好", "开始", "使用说明"}:
            mode = "个人计费。发「绑定个人Key」或「解绑个人Key」管理自己的绑定。" if personal else "本机器人使用组内共用计费。"
            return mode + "\n" + HELP
        if query in {"我的编号", "/whoami"}:
            return f"你在此应用中的 open_id 是 {user}。"
        if query in {"清除上下文", "/reset"}:
            return "已清除本服务此前保存的续聊上下文，飞书中的消息由飞书管理。"
        if query in {"安全培训", "课件目录", "安全课件", "安全课件目录"}:
            titles = [rule["title"] for rule in self.rules]
            return "本实例的提醒主题：" + "、".join(titles) if titles else "管理员尚未配置培训提醒。"
        if "模板" in query and len(query) <= 25 and not re.search("引导|带我|一步步|帮我记|填写|我做了", query):
            name = "GUV" if "guv" in query else "通用"
            return f"【{name}实验记录空白模板】\n{TEMPLATES[name]}"
        if re.search(r"安全|防护|风险|危险|课件|培训|注意什么|注意事项|防范", text) and not re.search("帮我记|记录下|记一下", text):
            action = upcoming_step(text)
            if action and not re.search(r"[？?]|怎么|如何|要注意|注意什么|有什么|安全(?:吗|提醒|提示|培训)|课件", text):
                return ""
            return self.match(action or text) or "本地规则尚未收录这项操作。请查本单位现行规程，或联系设备负责人。"
        return ""
