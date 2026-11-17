# 飞书实验记录助手

实验组成员在飞书单聊中口述样品、读数和更正，助手保留原话与单位。发送「记录完成」后，它把本次记录写进发言人自己的知识库目录，并回读正文后返回链接。公开版附带虚构的样品和安全提醒，方便在自己的团队里试用、修改和测试。

## 试一条模拟记录

在已配置的机器人单聊中，依次发送下面四条消息。

```text
开始记录 模拟称量
样品：DEMO-01；测量：10.0 g；原始文件：无，模拟演练。
更正：DEMO-01 的模拟读数应为 10.1 g。
记录完成
```

第三条和原读数都保留在正文中。中途发「查看记录」可以回看草稿。再次发送「记录完成」会返回上一份文档的链接。飞书文件名或本地路径按口述保留为索引；本版本的消息入口是文字单聊。手机上可先用飞书语音转文字再发送。

## 先在本地跑测试

在 macOS 或 Linux 上使用 Python 3.12。测试使用临时目录、虚构的成员和模拟的远端文档，无需飞书账号或 DeepSeek Key。安装 Node.js 可一并运行离线绑定页的 WebCrypto 测试。

```sh
git clone https://github.com/ericagaohh-design/feishu-lab-assistant.git
cd feishu-lab-assistant
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -v
```

## 接入自己的飞书应用

1. 在飞书开放平台创建企业自建应用，启用机器人，选择长连接接收事件。订阅 `im.message.receive_v1`，授予单聊消息读取和机器人回复所需权限，发布给自己的测试成员。[飞书长连接配置](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case) · [消息回复接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply)
2. 在运行服务的账号里提供 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_TENANT_KEY`。用部署环境的秘密配置传入凭证。程序通过环境变量读配置。私有状态目录由 `LAB_STATE_DIR` 指定，默认是 `~/.local/state/feishu-lab-assistant`，目录权限为 `0700`。
3. 默认按成员自己的 DeepSeek Key 调用模型。先在同一服务账号、同一状态目录下运行下面的命令。将生成的 HTML 通过团队认可的渠道分发，再将下载链接写入 `LAB_BIND_URL`。组员单聊发送「绑定个人Key」，在下载的本地表单中输入短时绑定码和个人 Key，然后只把生成的加密凭证发回该单聊。模板文件用于生成各实例自己的绑定页。[离线表单模板](bind-key.template.html)

   ```sh
   python enrollment.py export-form --output bind-key.html
   ```

4. 运行 `python bot.py`，向机器人发「帮助」或「通用模板」试收发。服务需运行在持续联网的主机上。每个部署实例使用自己的持久化状态目录，同时只运行一个进程。

模板、规则查询和知识库归档均可独立于模型使用。模型整理会把成员输入和近期相关上下文发送给 DeepSeek。服务加密保存成员 Key，代成员请求模型时在主机内解密，因此运行主机的管理员属于受信任方。模型可用 `DEEPSEEK_MODEL` 指定；团队主动选择集中计费时，把 `DS_KEY_MODE` 设为 `shared` 并配置 `DEEPSEEK_API_KEY`。

## 启用个人知识库归档

在自己的知识库中为每位成员建一个目录。授予机器人目标目录的新建和编辑权，并在开放平台开通知识库节点查询、列举和创建、Docx 读写，以及文档分享设置的读取和调整权限。机器人会在写正文前核实目标节点，并关闭新文档的公开链接和外部分享。原有协作者按目录授权访问。[创建知识库子节点](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/create)

将 [archive_routes.example.json](archive_routes.example.json) 复制到私有状态目录，命名为 `archive_routes.json`，或用 `LAB_ARCHIVE_ROUTES` 指向配置文件。将示例值替换成自己应用的 ID、租户、知识空间、成员目录根节点和各成员的子节点。每个 `open_id` 必须来自实际运行的同一个飞书应用；管理员须核对该身份和本人目录的对应关系。运行实例的成员映射应留在私有配置中。首次启用可用上面的模拟称量核对实际保存位置和权限。

## 共建实验模板与安全提醒

- **实验组员**可以提交一条模拟对话，写清「我怎么说、助手怎么回、希望它怎么回」。公开反馈只使用虚构样品、读数和文件名。[提交反馈](https://github.com/ericagaohh-design/feishu-lab-assistant/issues/new?template=feedback.yml)
- **模板维护者**可以修改通用或 GUV 记录的字段和追问，用模拟实验检查原话、单位与更正能否保留。[记录模板与提示](lab.py)
- **安全培训负责人**可以从虚构规则样例开始，为实际操作填写触发词、提醒原文和出处。用 `LAB_SAFETY_RULES` 指向团队自己的规则文件，更新后重启服务。团队内部 SOP 和课件请通过内部渠道维护。[模拟安全提醒](examples/safety_rules.json)
- **开发者**可以通过 Pull Request 提交改动和测试。GitHub Actions 会执行相同的离线测试；部署到各自的飞书应用由其维护者负责。

## 可借鉴的 GitHub 项目

- **LabBuddy** 把研究者的口述转成结构化字段，提示模型仅提取明确说出的内容，并标记尚缺的字段。适合研究如何追问而不补造读数。[字段抽取源码](https://github.com/abeckDev/LabBuddy-SmartLabProtocollationAssistant/blob/6936a938a112ba6adff2cd2fb4d912db0622bfe2/backend/agent.py#L24-L45)
- **eLabFTW 模板共建库** 展示了实验组如何提交模板、清理机构专属信息和审阅版本差异。[模板贡献指南](https://github.com/sfb1638/elabftw-templates/blob/e1d423c03c2199e01f561c6fa8342b135c3354b4/CONTRIBUTING.md)
- **飞书官方卡片机器人** 展示如何响应按钮回调并更新卡片。想把文字指令「查看记录」「记录完成」做成按钮，可以从这里读起。[Python 示例](https://github.com/larksuite/lark-samples/blob/de3d8b6d0cac128248df04f98a5eaa6c313e2569/card_interaction_bot/python/main.py)

本项目采用 [MIT 许可证](LICENSE)。
