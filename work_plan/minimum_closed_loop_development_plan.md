# 最小闭环开发方案

版本：v0.1
日期：2026-06-04
依据：`personal_wechat_module_development_details.md`

## 1. 最小闭环定义

第一轮最小闭环不直接操作真实微信，不发送真实消息。目标是先跑通一个可测试、可回放、可扩展工具的 dry-run 闭环：

```text
FakeWeChatDriver
  -> Message Normalizer
  -> Router & Deduper
  -> AI Topic Decision over recent 20 messages
  -> Conversation Engine
  -> FakeLLM or RealLLM
  -> Tool Runtime with Fake Tools
  -> Reply Gate dry_run
  -> Local Logs
  -> File Index
  -> Replay Test
```

最小闭环完成后，应能用一条命令回放一条私聊或群聊消息，并得到：

- 标准化消息。
- 路由决策。
- 是否调用工具的决策。
- 候选回复。
- dry-run 发送结果。
- JSONL 日志。
- 本地文件索引。
- 可重复运行的测试。

## 2. 第一轮不做

第一轮不做这些内容：

- 真实微信 UI 自动化。
- 真实微信发送。
- 群聊 `auto` 自动发言。
- 真实 PDF/DOCX 解析与公式渲染。
- 真实 Chrome/Google 浏览器自动化检索。
- 本地后台 UI。
- 长期记忆摘要生成的完整策略。
- 人设后台和每日轨迹自动生成。
- 手写配置文件作为唯一配置入口。

第一轮会预留接口，后续再接真实实现。

## 3. 最小交付物

### 3.1 项目骨架

文件：

- `app/personal_wechat_bot/domain/models.py`
- `app/personal_wechat_bot/config/schema.py`
- `app/personal_wechat_bot/config/loader.py`
- `app/personal_wechat_bot/control/cli.py`
- `app/personal_wechat_bot/control/commands.py`
- `app/personal_wechat_bot/bootstrap.py`
- `app/personal_wechat_bot/main.py`

交付：

- 通过 CLI/向导生成配置，不要求你手写 JSON。
- 能加载配置。
- 能创建核心数据模型。
- 配置错误能清晰失败。
- 能通过命令维护联系人白名单和群聊白名单。

### 3.2 Fake 微信输入

文件：

- `app/personal_wechat_bot/wechat_driver/base.py`
- `app/personal_wechat_bot/wechat_driver/fake.py`
- `tests/fixtures/messages/private_basic.json`
- `tests/fixtures/messages/group_topic_match.json`
- `tests/fixtures/messages/group_topic_miss.json`
- `tests/fixtures/messages/private_special_names.json`
- `tests/fixtures/messages/group_special_names.json`

交付：

- 不打开微信也能模拟私聊和群聊消息。
- fixture 可回放。
- fixture 覆盖常规中文名、特殊符号名和多语言名。

### 3.3 消息标准化和路由

文件：

- `app/personal_wechat_bot/normalizer/normalizer.py`
- `app/personal_wechat_bot/router/router.py`
- `app/personal_wechat_bot/router/deduper.py`

交付：

- 过滤自己发送的消息。
- 私聊白名单按微信号识别。
- 群聊白名单按群名识别。
- 提供群名修改工具，群名变化后对对应群生效。
- 重复消息不重复处理。

### 3.4 最小人设/topic 决策

文件：

- `app/personal_wechat_bot/persona/profile.py`
- `app/personal_wechat_bot/persona/topic_planner.py`
- `app/personal_wechat_bot/persona/topic_classifier.py`

交付：

- 私聊默认允许生成回复。
- 群聊必须经 AI 基于最近 20 句上下文判断 topic 后，才生成回复候选。
- topic 文件后续由你提供；最小闭环先用默认临时 topic 模板。
- 不使用关键词命中规则作为 topic 决策依据。
- 群聊同一群两次发言之间默认间隔 60 秒，且必须可配置。

### 3.5 会话引擎和 fake LLM

文件：

- `app/personal_wechat_bot/conversation/engine.py`
- `app/personal_wechat_bot/conversation/prompt_builder.py`
- `app/personal_wechat_bot/llm/base.py`
- `app/personal_wechat_bot/llm/fake.py`

交付：

- 用 fake LLM 生成固定候选回复。
- prompt 构造可测试。
- `silent` 决策不会调用 LLM。
- 回复风格为自然朋友聊天。
- 支持 plan 阶段、环节监控和最终总结。

### 3.6 工具运行时和 fake 工具

文件：

- `app/personal_wechat_bot/tools/base.py`
- `app/personal_wechat_bot/tools/registry.py`
- `app/personal_wechat_bot/tools/runtime.py`
- `app/personal_wechat_bot/tools/permissions.py`
- `app/personal_wechat_bot/tools/document/translator.py`
- `app/personal_wechat_bot/tools/search/external_search.py`
- `app/personal_wechat_bot/tools/search/model_relevance_filter.py`

交付：

- 能注册工具。
- 能从会话引擎触发工具调用。
- fake `document.translate` 返回一个本地 DOCX 文件引用。
- fake `document.translate` 支持文本输入和文件输入。
- fake `search.external_translate` 返回带来源 URL 的中文摘要。
- fake 模型相关性过滤层能过滤无关结果。
- 搜索结果长期保存。
- 搜索摘要必须是真摘要，不允许只是从开头截取一段。
- 提供工具访问搜索原文，由大模型自行决定是否调用。

### 3.7 dry-run 发送闸门和日志

文件：

- `app/personal_wechat_bot/reply_gate/gate.py`
- `app/personal_wechat_bot/logging/event_log.py`
- `app/personal_wechat_bot/memory/file_index.py`
- `app/personal_wechat_bot/replay/runner.py`

交付：

- dry-run 永远不调用真实发送。
- 每条消息处理链路都写 JSONL。
- 允许保存完整聊天文本。
- 为文件生成索引，便于后续工具访问。
- replay 能复现决策。

## 4. 推荐开发顺序

### Phase 0：确认最小闭环参数

先 grill 必要问题，确认后再开始写代码。

产出：

- 通过命令生成 `data/config.json` 初稿。
- 通过命令生成 `contacts_whitelist.json` 初稿。
- 通过命令生成 `groups_whitelist.json` 初稿。
- replay fixture 初稿。
- file index 初稿。

### Phase 1：配置和数据模型

实现：

- 配置 schema。
- 配置 CLI/向导。
- 白名单维护命令。
- 群名修改命令。
- 核心 domain models。
- 基础错误类型。

验收：

- 不手写 JSON，也能生成初始配置。
- 配置加载测试通过。
- 数据模型序列化/反序列化测试通过。
- 联系人白名单按微信号维护。
- 群聊白名单按群名维护。
- 群名修改后对应群配置生效。

### Phase 2：fake 输入到路由

实现：

- `FakeWeChatDriver`。
- normalizer。
- router。
- deduper。

验收：

- 白名单私聊进入处理。
- 非白名单私聊被忽略。
- 白名单群聊进入 topic 决策。
- 重复消息不重复处理。
- 小明、小刚、特殊符号名、日语/韩语/英语人名 fixture 均可标准化。
- 群名同样覆盖常规中文、特殊符号和多语言。

### Phase 3：会话引擎到 dry-run 回复

实现：

- AI topic planner。
- 最近 20 句上下文窗口。
- prompt builder。
- fake LLM。
- conversation engine。
- reply gate dry-run。
- JSONL logging。
- 环节监控和最终总结。

验收：

- 私聊 fixture 产生候选回复。
- 群聊 topic 由 AI 基于最近 20 句上下文判断。
- 群聊 topic 命中产生候选回复。
- 群聊 topic 不命中不生成发言候选。
- 同一群发言间隔默认 60 秒且可配置。
- dry-run 不发送。
- 回复是自然朋友聊天风格。
- 输出最终总结。

### Phase 4：工具调用闭环

实现：

- tool registry。
- tool runtime。
- tool permissions。
- fake document translate tool。
- fake external search tool。
- fake model relevance filter。
- file index。

验收：

- 用户消息触发 `document.translate` 时返回 DOCX 文件引用。
- `document.translate` 可接收文本输入和文件输入。
- 用户消息触发 `search.external_translate` 时返回摘要和来源 URL。
- 无关搜索结果被 fake model relevance filter 过滤。
- 搜索摘要由摘要器生成，不是简单截断。
- 搜索原文存本地并可通过工具访问。
- 搜索结果进入长期存储。
- 工具调用写入日志。

### Phase 5：真实 LLM 接入

实现：

- `llm/openai_client.py`。
- 中转站 base URL 配置。
- 长任务 plan 阶段。
- 环节监控。
- 超长等待状态发言。
- 异常停滞检测和失败回复。
- 开发环境详细错误回执。

验收：

- 使用 `gpt-5.5` 生成回复。
- 具体 base URL 和模型由你配置。
- API key 不入日志。
- 不做固定最大等待时间限制。
- 复杂任务长时间执行时能说明当前进度。
- 异常导致工作持续未推进时中断并给出失败回复。
- 最终回复总结内容。
- 开发环境有详细错误回执。
- 真实 LLM 可以替换 fake LLM，不影响上游模块。

## 5. 最小闭环验收命令草案

命令名后续可调整，目标是形成类似流程：

```text
bot replay tests/fixtures/messages/private_basic.json --mode dry_run
bot replay tests/fixtures/messages/group_topic_match.json --mode dry_run
bot replay tests/fixtures/messages/group_topic_miss.json --mode dry_run
bot replay tests/fixtures/messages/private_special_names.json --mode dry_run
bot replay tests/fixtures/messages/group_special_names.json --mode dry_run
bot replay tests/fixtures/messages/tool_document_translate.json --mode dry_run
bot replay tests/fixtures/messages/tool_external_search.json --mode dry_run
```

验收结果：

- 控制台输出处理摘要。
- `data/logs.jsonl` 追加日志。
- `data/tool_outputs/` 出现 fake 工具输出引用。
- `data/file_index.sqlite` 或等价索引存储出现文件索引。
- 所有测试可重复运行。

## 6. 最小闭环测试清单

### T01 配置测试

- 缺少配置时报错。
- `mode=dry_run` 可启动。
- API key 不从 `config.json` 读取。
- 初始配置由 CLI/向导生成。
- 白名单和群名可通过命令维护。

### T02 消息测试

- 私聊消息标准化。
- 群聊消息标准化。
- 自己发送的消息被过滤。
- 重复消息被过滤。
- 中文、特殊符号、日语、韩语、英语人名可处理。
- 中文、特殊符号、日语、韩语、英语群名可处理。

### T03 路由测试

- 白名单私聊通过。
- 非白名单私聊忽略。
- 白名单群聊进入 topic 决策。
- 非白名单群聊忽略。

### T04 topic 测试

- AI 基于最近 20 句判断 topic。
- topic 命中返回 `speak`。
- topic 不命中返回 `silent`。
- 冷却时间默认 60 秒且可配置。

### T05 回复测试

- fake LLM 返回固定回复。
- dry-run 不发送。
- 候选回复写入日志。
- 输出 plan、监控摘要和最终总结。

### T06 工具测试

- 工具能注册。
- 未注册工具调用失败并写日志。
- fake 文档翻译返回 DOCX 文件引用。
- fake 文档翻译支持文本和文件输入。
- fake 外网搜索返回来源 URL。
- fake 模型过滤层丢弃无关结果。
- 搜索结果长期存储。
- 搜索摘要不是头部截断。
- 搜索原文可通过工具读取。

## 7. 已确认参数

### Phase 0 已确认

- 配置文件第一版不接受手写 JSON，需要 CLI/向导或工具生成。
- 白名单联系人使用微信号。
- 白名单群聊使用群名。
- 需要提供群名修改工具，群名变化后对每个群名生效。
- fixture 人名包含：小明、小刚、特殊符号名、日语名、韩语名、英语名。
- fixture 群名同样覆盖：常规中文、特殊符号、日语、韩语、英语。
- 日志允许保存完整聊天文本。
- 文件需要生成索引，便于后续工具访问。
- 长期记忆第一版保存原文和摘要。

### Phase 3 已确认

- 回复风格为自然朋友聊天。
- topic 由你后续提供文件；最小闭环先自由设置临时模板。
- 实机测试时必须替换成你的 topic 设计。
- topic 不使用关键词规则。
- topic 由 AI 根据最近 20 句上下文判断。
- 群聊冷却时间可调整，暂定同一群两条发言之间间隔 1 分钟。

### Phase 4 已确认

- 工具调用通过命令触发。
- `data/inbox/` 作为文档工具默认输入目录。
- 工具输入需要支持文本和文件。
- fake 文档翻译输出文件名格式：`原文件名 + 翻译`。
- 搜索结果长期存储。
- 搜索常态暴露完整摘要。
- 摘要必须是真摘要，不允许只是从开头截取。
- 提供工具访问搜索原文。
- 大模型自行决定是否调用原文访问工具。
- blocklist 第一版使用默认名单，后续切换为自定义。

### Phase 5 已确认

- 使用中转站，由你配置具体 base URL 和模型。
- API key 环境变量名可使用 `OPENAI_API_KEY`。
- 最大等待时间不限。
- 需要各环节监控。
- 复杂任务等待超长时间时，机器人可发言说明当前状态。
- 异常导致工作持续未推进时，中断并给出失败回复。
- 不做流式输出。
- 需要 plan 阶段、环节监控和总结模块。
- 最后回复总结内容。
- 开发时需要详细报错回执。
- 正式落地时不要暴露详细报错回执。

## 8. 后续仍需 grill 的内容

你已说明这些在真实落地前再回复。

### 真实微信阶段前必答

这些不影响最小闭环，但进入真实微信 UI 前必须回答。

- 微信桌面版具体版本号。
- Windows 显示缩放比例。
- 是否允许机器人运行时占用微信窗口。
- 是否有多显示器。
- 机器人运行时是否允许你手动操作微信窗口。

### 真实文档工具前必答

这些不影响 fake 工具闭环，但进入真实 PDF/DOCX 解析前必须回答。

- 是否需要保留原文和译文对照？
- 文献翻译是否需要固定术语表？
- 长书籍翻译是否接受异步任务，不在微信里直接返回全文？

### 真实搜索工具前必答

这些不影响 fake 搜索闭环，但进入真实 Chrome/Google 自动化前必须回答。

- Chrome 自动化是否允许打开一个独立浏览器窗口？
- 是否允许使用当前 Chrome 用户配置，还是要用独立临时 profile？
- Google 搜索结果是否需要打开网页正文，还是第一版只处理搜索结果页标题和摘要？

## 9. 最小闭环完成标准

当以下条件全部满足，即认为最小闭环完成：

- 所有 Phase 1-4 的单元测试通过。
- replay 私聊消息能生成 dry-run 候选回复。
- replay 群聊 topic 命中消息能生成 dry-run 候选回复。
- replay 群聊 topic 不命中消息不会调用 LLM。
- replay 文档工具请求能返回 DOCX 文件引用。
- replay 外网搜索工具请求能返回带 URL 的中文摘要。
- replay 外网搜索工具请求能保存摘要和原文引用。
- replay 工具文件能进入文件索引。
- 日志能解释每条消息为什么回复、为什么不回复、是否调用工具。
- 不依赖真实微信。
- 不发送任何真实微信消息。

## 10. 最小闭环之后的下一步

建议顺序：

1. 接真实 `gpt-5.5`。
2. 接真实 PDF/DOCX 解析和 DOCX 公式渲染。
3. 接真实 Chrome/Google 搜索。
4. 接 Windows 微信只读监听。
5. 做 confirm 发送。
6. 最后再讨论群聊 auto。
