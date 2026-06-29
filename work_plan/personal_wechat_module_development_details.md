# 个人微信聊天机器人模块开发细节

版本：v0.1
日期：2026-06-04
用途：后续开发的实时指导文件；每次模块实现、debug 或设计调整后都应同步更新本文。

## 1. 当前已确认约束

- 运行平台：Windows。
- 微信客户端：微信桌面版。
- 微信控制方式：接受桌面 UI 自动化。
- 第一阶段发送模式：`dry_run`，只生成回复和日志，不自动发送。
- 私聊范围：只处理白名单联系人，白名单联系人按微信号维护。
- 群聊范围：需要实时监听；白名单群聊按群名维护，并提供群名修改工具；机器人根据人设背景、每日轨迹和当前 topic 自主判断是否发言。
- 模型：`gpt-5.5` API，已有 key。
- 模型接入：使用中转站，base URL 和模型由你配置；API key 环境变量可使用 `OPENAI_API_KEY`。
- 模型交互：不做流式输出；最大等待时间不限；需要 plan 阶段、环节监控和总结模块。
- 记忆：保存三周以内完整聊天记录和摘要；长期记忆用于稳定说话风格。
- 文件：需要为文件生成索引，便于后续工具访问。
- 文档工具：第一版支持 PDF 和 DOCX；输出 DOCX。
- 文档公式：PDF 公式主要按可复制 LaTeX 处理；需要完善的数学 LaTeX 渲染能力，公式在 DOCX 中必须正确排版。
- 搜索工具：走浏览器自动化；跟随默认浏览器 Chrome；搜索入口使用 Google；增加模型相关性过滤层，过滤无关内容。
- 搜索存储：搜索结果长期存储；常态暴露完整摘要；摘要必须是真摘要，不允许只是截取开头。
- 工具调用：命令触发；工具输入支持文本和文件；`data/inbox/` 是文档工具默认输入目录。
- 工具输出：前台默认发送摘要和来源 URL；完整内容存本地；用户明确要求全内容时返回文件引用。
- fixture：测试人名和群名需覆盖小明、小刚、特殊符号名、日语、韩语、英语。
- 不做：微信公众号、企业微信、云端生产部署、群发营销、朋友圈、红包、转账、支付、加好友、协议逆向、反检测。

## 2. 仍需 grill 的关键问题

这些问题不阻止先写开发细节，但在进入对应模块实现前必须确认。

- 微信桌面版具体版本号。
- Windows 显示缩放比例，例如 100%、125%、150%。
- 机器人运行时是否允许用户手动操作微信窗口。
- 群聊最终是否允许 `auto` 自动发送，还是长期保持 `confirm`。
- 每日轨迹由你手写、脚本生成，还是模型每天自动生成。
- 是否有任何联系人、群聊、话题或内容绝对不能保存。

## 3. 暂定技术栈

此处是开发细节文件的默认假设，后续可按你的偏好替换。

- 语言：Python。
- GUI 自动化：Windows UI Automation / pywinauto / 可替换 driver。
- API 调用：OpenAI SDK 或 OpenAI-compatible HTTP client。
- 数据模型：Pydantic 或 dataclass。
- 本地存储：SQLite + JSON/Markdown 配置文件。
- 测试：pytest。
- 命令行控制：Typer 或 argparse。
- 日志：JSONL 结构化日志。
- 工具层：统一 Tool Registry + 可替换工具实现。
- 文档处理：PDF/DOCX 优先；输出 DOCX；公式需要可渲染数学对象。
- 外网检索：浏览器自动化 provider；不硬绑定国内搜索源。

选择 Python 的理由：

- Windows 桌面 UI 自动化生态成熟。
- SQLite、文件配置、fixture 回放都轻。
- 后续如果要加本地小后台，可再加 FastAPI，不影响核心模块。

## 4. 建议目录结构

```text
app/
  personal_wechat_bot/
    __init__.py
    main.py
    bootstrap.py
    domain/
      __init__.py
      models.py
      enums.py
      errors.py
    config/
      loader.py
      schema.py
      defaults.py
    wechat_driver/
      base.py
      fake.py
      windows_ui.py
      parser.py
    normalizer/
      normalizer.py
    router/
      deduper.py
      router.py
    conversation/
      engine.py
      prompt_builder.py
    llm/
      base.py
      openai_client.py
      fake.py
    memory/
      store.py
      sqlite_store.py
      summarizer.py
      retention.py
      file_index.py
    policy/
      rules.py
      rate_limit.py
      sensitive.py
    reply_gate/
      gate.py
      confirmer.py
    persona/
      profile.py
      daily_trace.py
      topic_planner.py
    tools/
      __init__.py
      base.py
      registry.py
      runtime.py
      permissions.py
      document/
        parser.py
        translator.py
        chunker.py
        glossary.py
        docx_writer.py
        math_renderer.py
      search/
        provider_base.py
        browser_provider.py
        external_search.py
        query_planner.py
        result_filter.py
        model_relevance_filter.py
        result_translator.py
    control/
      cli.py
      commands.py
    logging/
      audit.py
      event_log.py
    replay/
      runner.py
      fixtures.py
tests/
  unit/
  integration/
  fixtures/
data/
  config.json
  contacts_whitelist.json
  groups_whitelist.json
  persona_profile.md
  daily_traces.jsonl
  topic_rules.json
  tool_config.json
  search_blocklist.json
  glossary.json
  inbox/
  tool_outputs/
  conversations.sqlite
  logs.jsonl
  replay_cases/
work_plan/
```

## 5. 运行模式

### 5.1 dry_run

行为：

- 监听消息。
- 生成候选回复。
- 记录日志。
- 不向微信发送。

用途：

- 第一阶段默认模式。
- 验证消息识别、去重、上下文、prompt 和模型效果。

### 5.2 confirm

行为：

- 监听消息。
- 生成候选回复。
- 等待用户确认。
- 用户确认后才发送。

用途：

- 验证发送链路。
- 观察群聊发言是否自然。

### 5.3 auto

行为：

- 私聊：仅对白名单联系人自动发送。
- 群聊：仅对白名单群聊，且必须通过 topic、人设、频控和发送闸门。

用途：

- 后期稳定后再启用。

## 6. 核心数据模型

### 6.1 RawWeChatMessage

来源于微信驱动，尽量保留原始观测字段。

```json
{
  "raw_id": "string",
  "chat_title": "string",
  "sender_name": "string",
  "text": "string",
  "is_self": false,
  "is_group": false,
  "observed_at": "2026-06-04T12:00:00+08:00",
  "driver_meta": {
    "window_handle": "string",
    "source": "windows_ui"
  }
}
```

### 6.2 NormalizedMessage

系统内部统一消息。

```json
{
  "message_id": "string",
  "conversation_id": "string",
  "conversation_type": "private",
  "chat_title": "string",
  "sender_name": "string",
  "text": "string",
  "is_self": false,
  "received_at": "2026-06-04T12:00:00+08:00"
}
```

`conversation_type` 可选：

- `private`
- `group`

### 6.3 RouteDecision

路由和去重后的处理决策。

```json
{
  "message_id": "string",
  "conversation_id": "string",
  "action": "process",
  "reason": "whitelist_matched",
  "requires_topic_decision": false
}
```

`action` 可选：

- `process`
- `ignore`
- `duplicate`
- `blocked`

### 6.4 SpeakDecision

人设与 topic 层输出。

```json
{
  "conversation_id": "string",
  "decision": "speak",
  "reason": "topic_matched",
  "topic": "string",
  "confidence": 0.82,
  "style_context": "string",
  "daily_trace_context": "string"
}
```

`decision` 可选：

- `speak`
- `silent`
- `wait`

### 6.5 ReplyCandidate

模型和会话引擎生成的候选回复。

```json
{
  "message_id": "string",
  "conversation_id": "string",
  "text": "string",
  "send_mode": "dry_run",
  "policy_hits": [],
  "model": "gpt-5.5",
  "created_at": "2026-06-04T12:00:03+08:00"
}
```

### 6.6 SendResult

发送闸门和微信驱动输出。

```json
{
  "message_id": "string",
  "conversation_id": "string",
  "status": "skipped",
  "reason": "dry_run",
  "sent_at": null
}
```

`status` 可选：

- `skipped`
- `queued_for_confirm`
- `sent`
- `failed`

### 6.7 ToolCallRequest

chatbot 内部请求工具执行的统一结构。

```json
{
  "tool_name": "document.translate",
  "call_id": "string",
  "conversation_id": "string",
  "requested_by": "chatbot",
  "arguments": {
    "input_path": "data/inbox/paper.pdf",
    "target_language": "zh-CN"
  },
  "created_at": "2026-06-04T12:00:00+08:00"
}
```

### 6.8 ToolCallResult

工具执行完成后的统一结果。

```json
{
  "call_id": "string",
  "tool_name": "document.translate",
  "status": "completed",
  "summary": "string",
  "output_refs": ["data/tool_outputs/paper.zh-CN.docx"],
  "error": null,
  "completed_at": "2026-06-04T12:03:00+08:00"
}
```

`status` 可选：

- `queued`
- `running`
- `completed`
- `failed`
- `blocked`

### 6.9 SearchResult

外网检索工具返回的单条结果。

```json
{
  "title": "string",
  "url": "https://example.com",
  "source_domain": "example.com",
  "language": "en",
  "published_at": null,
  "snippet": "string",
  "translated_title": "string",
  "translated_summary": "string",
  "relevance_score": 0.86,
  "spam_score": 0.02,
  "model_relevance": {
    "is_relevant": true,
    "reason": "string"
  }
}
```

## 7. 主循环流程

```text
load config
init logs
init memory store
init wechat driver
init llm client
init tool registry

while running:
  health_check wechat driver
  raw_messages = driver.read_new_messages()
  for raw in raw_messages:
    normalized = normalizer.normalize(raw)
    route_decision = router.decide(normalized)
    if route_decision.action != process:
      audit log and continue

    speak_decision = persona_topic_planner.decide(normalized, memory, config)
    if speak_decision.decision != speak:
      audit log and continue

    reply = conversation_engine.generate_reply(normalized, speak_decision)
    if reply requests tool:
      tool_result = tool_runtime.execute(tool_call_request)
      reply = conversation_engine.continue_with_tool_result(tool_result)
    gated = reply_gate.handle(reply)
    audit log

  sleep polling interval
```

第一阶段 `dry_run` 中，`reply_gate.handle()` 只会写日志，不会调用 `driver.send_message()`。

## 8. 模块开发细节

### M00 Config & Bootstrap：配置和启动

职责：

- 读取 `data/config.json`。
- 读取环境变量中的 API key。
- 校验配置完整性。
- 初始化各模块。
- 初始化工具注册中心。
- 提供统一启动入口。

建议文件：

- `app/personal_wechat_bot/config/schema.py`
- `app/personal_wechat_bot/config/loader.py`
- `app/personal_wechat_bot/bootstrap.py`
- `app/personal_wechat_bot/main.py`

核心接口：

```python
def load_config(path: str) -> BotConfig: ...
def build_app(config: BotConfig) -> BotRuntime: ...
def run_bot(runtime: BotRuntime) -> None: ...
```

配置校验：

- `mode` 必须是 `dry_run`、`confirm`、`auto` 之一。
- `retention_days` 默认为 21，不能超过 21，除非你明确确认。
- `private_chat_scope` 必须默认为 `whitelist_only`。
- `allow_group_chat` 为 true 时必须存在 `groups_whitelist.json`。
- API key 不能写在 `config.json`。
- 工具默认只能访问 `data/inbox/` 和 `data/tool_outputs/`。
- 外网搜索 provider 未确认时只能启用 fake provider。

测试：

- 缺失配置时报清晰错误。
- 无 API key 时真实 LLM client 不启动。
- `dry_run` 模式可以用 fake LLM 跑通。
- `retention_days > 21` 时默认报错。
- 不手写 JSON 也能通过 CLI/向导生成初始配置。
- API key 环境变量名支持 `OPENAI_API_KEY`。

需要 grill：

- 配置向导使用命令行交互，还是后续本地网页表单。

### M01 WeChat Driver：微信桌面驱动

职责：

- 定位微信桌面窗口。
- 读取当前可见聊天列表和新消息。
- 识别消息所属会话、发送者、文本、是否自己发送。
- 在非 `dry_run` 模式下发送文本。
- 提供健康检查。

建议文件：

- `app/personal_wechat_bot/wechat_driver/base.py`
- `app/personal_wechat_bot/wechat_driver/fake.py`
- `app/personal_wechat_bot/wechat_driver/windows_ui.py`
- `app/personal_wechat_bot/wechat_driver/parser.py`

核心接口：

```python
class WeChatDriver:
    def health_check(self) -> DriverHealth: ...
    def read_new_messages(self) -> list[RawWeChatMessage]: ...
    def focus_chat(self, conversation_id: str) -> None: ...
    def send_message(self, conversation_id: str, text: str) -> SendResult: ...
```

实现分层：

- `FakeWeChatDriver`：测试用，读取 fixture。
- `WindowsUIWeChatDriver`：真实桌面 UI 自动化。
- `MessageParser`：把 UI 节点或窗口文本转为 `RawWeChatMessage`。

第一阶段限制：

- 只实现 `FakeWeChatDriver`。
- `WindowsUIWeChatDriver.send_message()` 在 `dry_run` 下不被调用。
- 真实驱动实现前必须确认微信版本、缩放比例、是否允许用户同时操作窗口。

失败处理：

- 找不到微信窗口：进入 `driver_unavailable` 状态并记录日志。
- 微信未登录：不处理消息。
- UI 结构变化：停止真实发送，只保留日志。
- 读取到空消息：丢弃。

测试：

- fake driver 返回固定消息。
- fake driver 模拟重复消息。
- fake driver 模拟微信不可用。
- parser 过滤自己发送的消息。

需要 grill：

- 微信桌面版具体版本号。
- Windows 显示缩放比例。
- 是否能接受机器人运行时锁定/占用微信窗口。
- 是否有多显示器。

### M02 Message Normalizer：消息标准化

职责：

- 把 `RawWeChatMessage` 转为 `NormalizedMessage`。
- 生成稳定 `message_id`。
- 生成稳定 `conversation_id`。
- 过滤自己发送、空文本、系统提示。

建议文件：

- `app/personal_wechat_bot/normalizer/normalizer.py`
- `app/personal_wechat_bot/domain/models.py`

核心接口：

```python
def normalize(raw: RawWeChatMessage) -> NormalizedMessage | None: ...
```

ID 规则：

- `conversation_id = hash(conversation_type + chat_title)`。
- `message_id = hash(conversation_id + sender_name + text + observed_at_rounded)`。
- 时间可按秒或更粗粒度取整，具体需要测试 UI 读取稳定性。

过滤规则：

- `is_self == true` 不进入后续处理。
- `text.strip() == ""` 不处理。
- 微信系统提示不处理。
- 非文本消息第一版记录但不回复。

测试：

- 相同原始消息生成相同 `message_id`。
- 不同群聊同名发送者不会串会话。
- 自己发送消息被过滤。
- 非文本消息被记录为 unsupported。

需要 grill：

- 非文本消息，例如图片、语音、表情，第一版是否只记录不处理？

### M03 Router & Deduper：路由与去重

职责：

- 判断消息是否来自白名单联系人或白名单群。
- 去重。
- 根据私聊/群聊进入不同处理路径。
- 记录被忽略原因。

建议文件：

- `app/personal_wechat_bot/router/router.py`
- `app/personal_wechat_bot/router/deduper.py`

核心接口：

```python
def decide(message: NormalizedMessage, state: RouterState) -> RouteDecision: ...
def mark_processed(message_id: str) -> None: ...
```

路由规则：

- 私聊：必须命中 `contacts_whitelist.json`。
- 群聊：必须命中 `groups_whitelist.json`。
- 重复 `message_id`：返回 `duplicate`。
- 黑名单优先级高于白名单。

状态存储：

- MVP 可用 SQLite 表 `processed_messages`。
- 测试可用内存 set。

测试：

- 白名单私聊进入 `process`。
- 非白名单私聊进入 `ignore`。
- 白名单群聊进入 `process`，且 `requires_topic_decision=true`。
- 同一 `message_id` 第二次进入 `duplicate`。

需要 grill：

- 群名修改工具是否需要保留群名历史别名。

### M04 Persona & Topic Planner：人设、每日轨迹与话题决策

职责：

- 读取人设背景。
- 读取每日轨迹。
- 读取 topic 规则。
- 判断群聊中是否应该发言。
- 给会话引擎提供风格上下文。

建议文件：

- `app/personal_wechat_bot/persona/profile.py`
- `app/personal_wechat_bot/persona/daily_trace.py`
- `app/personal_wechat_bot/persona/topic_planner.py`

数据文件：

- `data/persona_profile.md`
- `data/daily_traces.jsonl`
- `data/topic_rules.json`
- topic 文件后续由你提供；最小闭环使用临时默认模板。

核心接口：

```python
def build_persona_context(message: NormalizedMessage) -> PersonaContext: ...
def decide_topic(message: NormalizedMessage, context: PersonaContext) -> SpeakDecision: ...
```

topic 规则草案：

```json
{
  "interest_topics": ["AI", "游戏", "日常闲聊"],
  "avoid_topics": ["隐私", "金钱请求"],
  "speak_when_mentioned": true,
  "speak_when_topic_confidence_above": 0.75,
  "group_cooldown_seconds": 60,
  "context_window_messages": 20,
  "decision_mode": "ai_context_classifier"
}
```

群聊发言决策顺序：

1. 是否白名单群。
2. 是否全局暂停或群聊暂停。
3. 是否在冷却时间内。
4. 是否被明确提及。
5. 由 AI 根据最近 20 句上下文判断是否命中 topic。
6. 是否触发禁区 topic。
7. 是否符合人设当前状态和每日轨迹。
8. 输出 `speak`、`silent` 或 `wait`。

第一版实现策略：

- 不使用关键词规则作为 topic 决策依据。
- 使用 AI topic classifier 根据最近 20 句上下文判断主题。
- topic 体系最小闭环可用临时模板，实机测试前必须替换成你提供的 topic 文件。

测试：

- AI 判断 topic 命中时返回 `speak`。
- 命中禁区 topic 返回 `silent`。
- 冷却期内返回 `wait`。
- topic 置信度不足返回 `silent`。
- 每日轨迹能进入 `style_context`。

需要 grill：

- 是否只在被 @ 或被叫昵称时强制提高发言优先级？
- 每日轨迹每天什么时候生成？
- 人设背景是否允许机器人主动“延续设定”，还是只能基于你提供的内容说？

### M05 Conversation Engine：会话引擎

职责：

- 整合消息、记忆、人设上下文、topic 决策。
- 判断用户是否在请求工具能力，例如翻译文档、检索外网资料。
- 构造 prompt。
- 发起工具调用并把工具结果纳入后续回复。
- 调用 LLM Client。
- 产出 `ReplyCandidate`。
- 在 `dry_run` 下仍记录完整候选回复。

建议文件：

- `app/personal_wechat_bot/conversation/engine.py`
- `app/personal_wechat_bot/conversation/prompt_builder.py`

核心接口：

```python
def generate_reply(
    message: NormalizedMessage,
    speak_decision: SpeakDecision,
    memory_context: MemoryContext,
) -> ReplyCandidate: ...

def continue_with_tool_result(
    message: NormalizedMessage,
    tool_result: ToolCallResult,
) -> ReplyCandidate: ...
```

prompt 组成：

- 系统规则：只作为个人聊天辅助，不做营销群发，不泄露内部配置。
- 人设：来自 `persona_profile.md`。
- 今日轨迹：来自 `daily_traces.jsonl` 的当天记录。
- 风格记忆：来自三周内摘要。
- 当前消息：用户或群聊消息。
- 可用工具清单：只暴露已注册且有权限的工具。
- 回复约束：短、自然、像真实聊天，不解释自己是机器人。

私聊处理：

- 白名单命中后默认可以生成回复。
- `dry_run` 不发送。
- `confirm` 等待确认。
- `auto` 仅后期开放。

群聊处理：

- 只有 `SpeakDecision.decision == "speak"` 才生成回复。
- 回复应短而自然。
- 避免连续多条。
- 不接管群聊。

测试：

- fake LLM 返回固定文本，生成 `ReplyCandidate`。
- `silent` 决策不会调用 LLM。
- prompt 包含人设和当天轨迹。
- 不同会话上下文隔离。
- 用户请求翻译文档时生成 `ToolCallRequest`。
- 工具返回结果后生成简短说明和输出引用。

需要 grill：

- 回复风格要更像“自然朋友聊天”，还是更像“有明确人格的角色扮演”？
- 群聊中是否允许机器人主动开启新话题？

### M06 LLM Client：大模型客户端

职责：

- 调用 `gpt-5.5` 或你在中转站配置的模型。
- 使用中转站 base URL，由你配置具体网址和模型。
- 管理超时、重试、错误分类。
- 记录模型、延迟、token 或用量摘要。
- 不记录 API key。
- 最大等待时间不限。
- 复杂任务需要 plan 阶段、环节监控和最终总结。
- 长时间执行时可发言说明进度。
- 异常导致工作持续未推进时中断并给出失败回复。
- 开发环境输出详细错误回执，正式落地不暴露详细报错。

建议文件：

- `app/personal_wechat_bot/llm/base.py`
- `app/personal_wechat_bot/llm/openai_client.py`
- `app/personal_wechat_bot/llm/fake.py`

核心接口：

```python
class LLMClient:
    def generate(self, request: LLMRequest) -> LLMResponse: ...
```

请求草案：

```json
{
  "model": "gpt-5.5",
  "system_prompt": "string",
  "messages": [],
  "timeout_seconds": 20,
  "metadata": {
    "conversation_id": "string"
  }
}
```

失败处理：

- 超时：返回降级回复或 `failed`。
- 限流：记录并退避。
- API key 错误：停止真实模型调用。
- 网络错误：可重试，最多 2 次。
- 任务长时间仍有进展：持续记录监控状态，不按固定超时中断。
- 任务长时间无进展：中断并返回失败回复。

测试：

- fake client 成功返回。
- fake client 超时。
- fake client 限流。
- fake client 错误不泄露 key。
- plan、监控和总结字段完整。
- 开发模式错误回执包含可 debug 信息。

需要 grill：

- 中转站 base URL 的配置方式和默认值。

### M07 Memory Store：本地记忆

职责：

- 保存三周以内的消息记录。
- 保存回复候选和发送结果。
- 生成/保存风格摘要。
- 保存原文和摘要。
- 按联系人和群聊隔离。
- 执行 21 天保留策略。
- 保存文件索引，便于工具访问文件。

建议文件：

- `app/personal_wechat_bot/memory/store.py`
- `app/personal_wechat_bot/memory/sqlite_store.py`
- `app/personal_wechat_bot/memory/summarizer.py`
- `app/personal_wechat_bot/memory/retention.py`

SQLite 表草案：

```sql
conversations(id, type, title, created_at, updated_at)
messages(id, conversation_id, sender_name, text, is_self, received_at)
reply_candidates(id, message_id, text, model, mode, created_at)
send_results(id, reply_id, status, reason, sent_at)
style_summaries(id, conversation_id, summary, updated_at)
processed_messages(message_id, processed_at)
file_index(id, original_name, stored_path, source, mime_type, sha256, created_at)
```

记忆策略：

- 原始消息保存不超过 21 天。
- 风格摘要可以保留，但需支持清空。
- 原文和摘要都保存，均遵守 21 天保留策略，除非后续另行确认。
- 文件索引用于定位工具输入和输出文件。

测试：

- 写入消息。
- 查询最近 N 轮。
- 超过 21 天清理。
- 指定联系人清空。
- 指定群聊清空。
- 文件索引可写入和查询。

需要 grill：

- 风格摘要是否可以超过三周保留。

### M08 Policy：基础策略

职责：

- 控制全局开关。
- 控制联系人/群聊开关。
- 频控。
- 敏感词和禁区 topic。
- 发送模式限制。
- 群聊发言冷却。

建议文件：

- `app/personal_wechat_bot/policy/rules.py`
- `app/personal_wechat_bot/policy/rate_limit.py`
- `app/personal_wechat_bot/policy/sensitive.py`

核心接口：

```python
def evaluate_before_llm(message: NormalizedMessage) -> PolicyResult: ...
def evaluate_before_send(reply: ReplyCandidate) -> PolicyResult: ...
```

默认策略：

- 非白名单不处理。
- 群聊必须白名单。
- 群聊必须经过 topic planner。
- 每个群有冷却时间，默认 60 秒。
- `dry_run` 永不发送。
- 命中敏感词时只记录不发。

测试：

- 全局暂停阻断。
- 单联系人暂停阻断。
- 冷却时间阻断。
- 敏感词阻断。
- `dry_run` 阻断真实发送。

需要 grill：

- 初始敏感词和禁区 topic 由你提供，还是先用空列表。

### M09 Reply Gate：发送闸门

职责：

- 决定 `ReplyCandidate` 的最终动作。
- `dry_run`：记录，不发送。
- `confirm`：排队等待确认。
- `auto`：通过策略后发送。
- 所有动作写入 `SendResult`。

建议文件：

- `app/personal_wechat_bot/reply_gate/gate.py`
- `app/personal_wechat_bot/reply_gate/confirmer.py`

核心接口：

```python
def handle(reply: ReplyCandidate, mode: SendMode) -> SendResult: ...
def confirm(reply_id: str) -> SendResult: ...
def cancel(reply_id: str) -> SendResult: ...
```

confirm 模式 MVP：

- 命令行打印候选回复。
- 输入 `y` 发送，输入 `n` 取消。
- 后续可迁移到本地后台。

测试：

- `dry_run` 永远不调用 driver。
- `confirm` 进入等待。
- `auto` 在策略通过时调用 fake driver。
- 发送失败返回 `failed`。

需要 grill：

- confirm 模式你希望命令行确认，还是本地网页确认？
- 群聊是否永远需要 confirm，还是后期可 auto？

### M10 Local Control：本地控制

职责：

- 启动、停止机器人。
- 切换 `dry_run`、`confirm`、`auto`。
- 管理白名单。
- 通过命令生成和修改配置，不要求手写 JSON。
- 联系人白名单按微信号维护。
- 群聊白名单按群名维护。
- 提供群名修改命令。
- 暂停指定联系人或群聊。
- 清理记忆。
- 查看最近候选回复和日志。

建议文件：

- `app/personal_wechat_bot/control/cli.py`
- `app/personal_wechat_bot/control/commands.py`

MVP 命令草案：

```text
bot run --mode dry_run
bot status
bot whitelist add-contact "备注名"
bot whitelist add-group "群名"
bot whitelist rename-group "旧群名" "新群名"
bot pause --conversation "群名"
bot resume --conversation "群名"
bot memory clear --conversation "备注名"
bot replay data/replay_cases/case_001.json
```

测试：

- 命令解析。
- 配置文件更新。
- 无需手写 JSON 也能完成初始配置。
- 微信号白名单更新。
- 群名修改生效。
- 暂停状态生效。
- 清理记忆生效。

需要 grill：

- 配置向导使用命令行交互，还是后续本地网页表单。

### M11 Local Logs & Replay：日志和回放

职责：

- 记录每条消息的处理链路。
- 支持排查为什么回复或为什么没回复。
- 支持用历史消息回放。
- 支持脱敏。

建议文件：

- `app/personal_wechat_bot/logging/audit.py`
- `app/personal_wechat_bot/logging/event_log.py`
- `app/personal_wechat_bot/replay/runner.py`
- `app/personal_wechat_bot/replay/fixtures.py`

日志事件类型：

- `driver.health`
- `message.raw`
- `message.normalized`
- `route.decision`
- `topic.decision`
- `llm.request`
- `llm.response`
- `reply.candidate`
- `send.result`
- `tool.call`
- `tool.result`
- `policy.blocked`
- `error`

日志字段：

```json
{
  "event_id": "string",
  "event_type": "reply.candidate",
  "message_id": "string",
  "conversation_id": "string",
  "timestamp": "2026-06-04T12:00:00+08:00",
  "payload": {}
}
```

回放流程：

1. 读取 replay case。
2. 使用 fake driver 投喂消息。
3. 使用 fake 或真实 LLM。
4. 比较决策结果，而不是强制比较模型文本完全一致。

测试：

- 日志写入 JSONL。
- API key 脱敏。
- replay 能复现 route/topic/reply 决策。
- 日志允许保存完整聊天文本。

### M12 Tool Runtime & Registry：工具运行时和注册中心

职责：

- 为 chatbot 提供统一工具调用入口。
- 管理工具注册、工具能力描述、参数 schema、权限策略和执行日志。
- 让后续二次开发新增工具时只需要实现统一接口。
- 支持同步短任务和异步长任务。
- 把工具结果返回给会话引擎，供机器人继续回复。
- 工具调用通过命令触发。
- 工具输入支持文本和文件，尽量和真人可执行操作对齐。

建议文件：

- `app/personal_wechat_bot/tools/base.py`
- `app/personal_wechat_bot/tools/registry.py`
- `app/personal_wechat_bot/tools/runtime.py`
- `app/personal_wechat_bot/tools/permissions.py`

核心接口：

```python
class Tool:
    name: str
    description: str
    argument_schema: dict

    def validate(self, arguments: dict) -> None: ...
    def run(self, request: ToolCallRequest) -> ToolCallResult: ...

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def list_tools(self) -> list[ToolManifest]: ...

class ToolRuntime:
    def execute(self, request: ToolCallRequest) -> ToolCallResult: ...
```

工具 manifest 草案：

```json
{
  "name": "document.translate",
  "description": "解析并翻译长文档",
  "version": "0.1.0",
  "input_schema": {},
  "output_schema": {},
  "permissions": ["read_local_file", "write_tool_output", "llm_call"],
  "supports_async": true
}
```

权限原则：

- 默认不允许工具读取任意路径。
- 文档工具只读取 `data/inbox/` 或你明确授权的路径。
- 输出默认写入 `data/tool_outputs/`。
- 搜索工具只走配置的外网 search provider。
- 工具调用必须写入审计日志。
- 前台默认只发送摘要、来源 URL 和本地输出引用。
- 完整内容存本地；只有用户明确要求全内容时，才由 Reply Gate 返回文件引用。

同步/异步策略：

- 短搜索可同步返回。
- 长书籍翻译必须作为长任务，返回 job id 或输出引用。
- 长任务进度写入本地状态，避免中途崩溃后完全丢失。

测试：

- 工具注册和发现。
- 参数 schema 校验。
- 未授权路径被阻断。
- 工具异常被包装成 `ToolCallResult(status="failed")`。
- 长任务状态可查询。

需要 grill：

- 命令格式是否固定为 `#翻译`、`#检索`，还是后续你会设计一套命令语法？

### M13 Document Parse & Translate Tool：文档解析与翻译工具

职责：

- 解析用户提供的长文档。
- 支持论文、文献、长书籍的分块翻译。
- 保持术语一致性。
- 尽量保留章节结构、标题、脚注、页码或段落顺序。
- 输出可继续检索、摘要和引用的结构化结果。
- 输出 DOCX 文件。
- 数学公式必须在 DOCX 中正确渲染，不允许只输出不可读的 LaTeX 文本作为最终形态。

建议文件：

- `app/personal_wechat_bot/tools/document/parser.py`
- `app/personal_wechat_bot/tools/document/chunker.py`
- `app/personal_wechat_bot/tools/document/translator.py`
- `app/personal_wechat_bot/tools/document/glossary.py`
- `app/personal_wechat_bot/tools/document/docx_writer.py`
- `app/personal_wechat_bot/tools/document/math_renderer.py`

首批支持格式：

- `.pdf`
- `.docx`

后续可选：

- `.epub`
- `.txt`
- `.md`
- OCR PDF。
- 扫描版书籍。
- 表格和公式保真。
- 双语对照导出。

核心工具：

```text
document.parse
document.translate
document.translate_section
document.summarize
document.build_glossary
```

`document.parse` 输入草案：

```json
{
  "input_path": "data/inbox/paper.pdf",
  "preserve_structure": true,
  "extract_metadata": true
}
```

`document.translate` 输入草案：

```json
{
  "input_path": "data/inbox/paper.pdf",
  "input_text": null,
  "source_language": "auto",
  "target_language": "zh-CN",
  "output_format": "docx",
  "bilingual": false,
  "glossary_path": "data/glossary.json",
  "chunk_size": 2500,
  "math_mode": "rendered_docx_equations"
}
```

输出草案：

```json
{
  "status": "completed",
  "document_id": "string",
  "title": "string",
  "detected_language": "en",
  "output_refs": [
    "data/tool_outputs/paper.zh-CN.docx"
  ],
  "warnings": []
}
```

解析流程：

1. 校验输入路径和文件类型。
2. 提取元数据。
3. 提取文本和结构。
4. 分章节或分段 chunk。
5. 记录 chunk id、页码或章节引用。
6. 写入中间结构文件，供翻译和检索复用。

翻译流程：

1. 解析文档。
2. 构建术语表。
3. 分块翻译。
4. 识别并保护公式片段。
5. 对前后 chunk 做风格和术语一致性检查。
6. 将译文、结构和公式合并成 DOCX。
7. 写入翻译日志和输出文件。

数学公式渲染流程：

1. 解析源文档中的公式。
2. 将可识别的 LaTeX 公式保留为结构化 math block。
3. 将 DOCX 原生公式尽量保留为可转换结构。
4. 生成目标 DOCX 时，将公式转换为 Word 可渲染的数学对象。
5. 第一版假设 PDF 公式为可复制 LaTeX；如果遇到图片或扫描公式，必须标记为 `math_ocr_required`，不能假装已正确解析。

公式验收要求：

- 行内公式和块级公式都应可区分。
- DOCX 打开后公式应以数学排版显示。
- 公式编号、引用和上下文尽量保留。
- 无法识别的公式必须在 warnings 中列出页码或位置。

长文档策略：

- 不把整本书一次性塞进模型。
- 按章节和语义段落切块。
- 每个 chunk 保留上下文摘要。
- 术语表在全书级别复用。
- 失败 chunk 可单独重试。

版权和数据边界：

- 默认只处理你有权处理的本地文件。
- 输出保存在本地。
- chatbot 不应在微信里直接发送大段书籍全文，只发送摘要、进度或本地输出引用。
- 输入既可以是 `data/inbox/` 中的文件，也可以是命令传入的文本。
- 输出文件名第一版使用 `原文件名 + 翻译.docx`。

测试：

- 小 PDF fixture 解析。
- DOCX 标题和段落保留。
- 长文本 chunk 不超过配置长度。
- fake LLM 翻译固定 chunk。
- 某个 chunk 失败后可重试。
- 术语表能影响后续翻译。
- 输出文件为 DOCX。
- 输出文件名符合 `原文件名 + 翻译.docx`。
- 文本输入可以生成翻译 DOCX。
- 文件输入可以生成翻译 DOCX。
- LaTeX 行内公式写入 DOCX 后可渲染。
- LaTeX 块级公式写入 DOCX 后可渲染。
- 无法解析的 PDF 图片公式会写入 warning。

需要 grill：

- 是否需要保留原文和译文对照？
- 文献翻译是否需要固定术语表？
- 长书籍翻译是否接受异步任务，不在微信里直接返回全文？

### M14 External Search & Translate Tool：外网检索与结果翻译工具

职责：

- 根据用户问题生成外网检索关键词。
- 避免国内词条、百科搬运、广告垃圾和低质量 SEO 页面。
- 常态按外网语境检索，再把结果翻译成中文摘要。
- 返回来源链接、英文标题、中文标题、中文摘要和可信度提示。
- 支持后续作为 chatbot 的外部知识工具。
- 通过浏览器自动化检索。
- 新增模型相关性过滤层，过滤无关内容和弱相关页面。

建议文件：

- `app/personal_wechat_bot/tools/search/provider_base.py`
- `app/personal_wechat_bot/tools/search/browser_provider.py`
- `app/personal_wechat_bot/tools/search/external_search.py`
- `app/personal_wechat_bot/tools/search/query_planner.py`
- `app/personal_wechat_bot/tools/search/result_filter.py`
- `app/personal_wechat_bot/tools/search/model_relevance_filter.py`
- `app/personal_wechat_bot/tools/search/result_translator.py`

核心工具：

```text
search.external
search.external_translate
search.related_content
search.open_source
```

`search.external` 输入草案：

```json
{
  "query": "string",
  "target_language": "zh-CN",
  "search_language": "en",
  "region": "global",
  "max_results": 8,
  "avoid_domestic_sources": true,
  "translate_results": true
}
```

输出草案：

```json
{
  "query_used": "english query string",
  "results": [
    {
      "title": "string",
      "url": "https://example.com",
      "translated_title": "string",
      "translated_summary": "string",
      "source_domain": "example.com",
      "relevance_score": 0.86,
      "spam_score": 0.02,
      "model_relevance": {
        "is_relevant": true,
        "reason": "string"
      }
    }
  ]
}
```

检索策略：

1. 如果用户输入是中文，先生成英文检索 query。
2. 默认 `search_language="en"`，`region="global"`。
3. 使用浏览器自动化 search provider。
4. 过滤国内词条站、百科搬运站、广告站、内容农场。
5. 使用模型相关性过滤层判断页面是否和原问题相关。
6. 对结果标题和摘要做中文翻译。
7. 返回链接和简短中文摘要，不伪装成已阅读全文。
8. 搜索结果长期存储。
9. 提供 `search.open_source` 访问原文，大模型自行决定是否调用。

默认 blocklist 草案：

```json
{
  "blocked_domains": [
    "baidu.com",
    "baike.baidu.com",
    "zhihu.com",
    "csdn.net",
    "sohu.com",
    "163.com",
    "qq.com"
  ],
  "blocked_title_keywords": [
    "广告",
    "推广",
    "十大",
    "排行榜"
  ]
}
```

质量规则：

- 优先英文原始来源、论文、官方文档、项目主页、学术机构、出版方、新闻原文。
- 不优先中文百科、搬运站、论坛营销帖。
- 明确区分搜索摘要、网页正文摘要和模型推断。
- 如果没有可用外网结果，直接说明未找到，不用国内垃圾结果填充。
- 模型过滤层必须输出保留/丢弃原因。
- 摘要必须包含来源 URL。
- 常态暴露完整摘要。
- 摘要必须是真摘要，不允许只是从开头截取一段。
- 全内容默认存本地，前台只发送摘要；用户明确要求全内容时返回本地文件引用。
- 默认 blocklist 第一版使用内置名单，后续切换为自定义。

工具 provider 设计：

- `SearchProvider` 只定义接口，不绑定具体服务。
- 首个真实 provider 使用浏览器自动化。
- 浏览器自动化 provider 跟随系统默认浏览器；当前已确认为 Chrome。
- 搜索入口使用 Google。
- 浏览器自动化 provider 负责打开 Google、读取搜索结果、提取标题/URL/snippet。
- 本工具不负责管理 VPN，只假设运行环境已经具备外网访问。

模型相关性过滤层：

- 输入：原问题、规划后的英文 query、候选标题、URL、snippet、可选网页正文片段。
- 输出：`keep`、`drop`、`uncertain`。
- 丢弃原因：广告、国内垃圾源、弱相关、标题党、内容农场、重复结果。
- `uncertain` 默认不直接丢弃，可降低排序并保留原因。

测试：

- 中文 query 被转换为英文 query。
- blocklist 域名被过滤。
- 广告标题关键词被降权或过滤。
- fake provider 返回英文结果后能生成中文摘要。
- 模型过滤层能丢弃无关结果。
- 摘要包含来源 URL。
- 摘要不是简单头部截断。
- 搜索结果长期存储。
- 原文可通过 `search.open_source` 访问。
- 无结果时返回空结果和清晰原因。

需要 grill：

- 自定义 blocklist 的配置格式后续是否由你提供。

## 9. 跨模块验收用例

### C01 私聊 dry_run

输入：

- 白名单联系人发来一句文本。
- 模式为 `dry_run`。

期望：

- 消息被读取。
- 通过白名单。
- 调用 fake 或真实 LLM。
- 生成候选回复。
- 不发送微信。
- 写入日志。

### C02 非白名单私聊

输入：

- 非白名单联系人发来消息。

期望：

- route 决策为 `ignore`。
- 不调用 LLM。
- 不发送。
- 写入忽略原因。

### C03 白名单群聊 topic 不匹配

输入：

- 白名单群聊出现无关话题。

期望：

- 消息被监听。
- topic planner 返回 `silent`。
- 不调用 LLM 或只调用轻量 topic classifier。
- 不发送。

### C04 白名单群聊 topic 匹配

输入：

- 白名单群聊出现匹配 topic。
- 不在冷却期。

期望：

- topic planner 返回 `speak`。
- 会话引擎生成候选回复。
- `dry_run` 下不发送。
- 写入候选回复。

### C05 重复消息

输入：

- 同一条消息被读取两次。

期望：

- 第一次处理。
- 第二次 route 决策为 `duplicate`。
- 不重复调用 LLM。

### C06 记忆保留

输入：

- 数据库中存在 22 天前消息。

期望：

- retention 清理原始记录。
- 保留策略按配置执行。

### C07 PDF/DOCX 长文档解析与 DOCX 翻译

输入：

- `data/inbox/` 中存在一份短 PDF 或 DOCX fixture。
- fixture 包含行内 LaTeX、块级 LaTeX 或 DOCX 公式。
- 用户或测试用例触发 `document.translate`。
- 使用 fake LLM。

期望：

- 工具层通过权限校验。
- 文档被解析和分块。
- fake LLM 对 chunk 返回固定译文。
- 输出 DOCX 写入 `data/tool_outputs/`。
- DOCX 中公式以可渲染数学对象出现。
- chatbot 只返回摘要和输出引用，不直接发送全文。

### C08 外网检索与结果翻译

输入：

- 用户触发 `search.external_translate`。
- fake browser search provider 返回英文结果、国内垃圾结果和无关结果混合列表。

期望：

- 中文 query 被转换或规划为外网 query。
- 国内 blocklist 结果被过滤。
- 模型相关性过滤层丢弃无关结果。
- 英文结果被翻译为中文标题和摘要。
- 摘要包含来源 URL。
- 全内容存入本地输出。
- 没有可用结果时明确说明，不用垃圾结果填充。

## 10. 开发顺序和文件落点

### Step 1：项目骨架和数据模型

文件：

- `app/personal_wechat_bot/domain/models.py`
- `app/personal_wechat_bot/config/schema.py`
- `app/personal_wechat_bot/config/loader.py`

验收：

- 能加载配置。
- 能创建核心数据对象。
- 单元测试通过。

### Step 2：fake 闭环

文件：

- `wechat_driver/fake.py`
- `llm/fake.py`
- `normalizer/normalizer.py`
- `router/router.py`
- `conversation/engine.py`
- `reply_gate/gate.py`
- `logging/event_log.py`

验收：

- 不打开微信、不接真实模型，可以完成 C01、C02。

### Step 3：SQLite 记忆和去重

文件：

- `memory/sqlite_store.py`
- `memory/retention.py`
- `router/deduper.py`

验收：

- 完成 C05、C06。

### Step 4：真实 LLM

文件：

- `llm/openai_client.py`

验收：

- 使用 `gpt-5.5` 生成回复。
- key 不入日志。
- 超时和错误可控。

### Step 5：人设和 topic 决策

文件：

- `persona/profile.py`
- `persona/daily_trace.py`
- `persona/topic_planner.py`
- `conversation/prompt_builder.py`

验收：

- 完成 C03、C04。

### Step 6：Windows 微信只读驱动

文件：

- `wechat_driver/windows_ui.py`
- `wechat_driver/parser.py`

验收：

- 能读取白名单私聊和群聊消息。
- 不发送。
- 不重复处理。

### Step 7：confirm 和发送

文件：

- `reply_gate/confirmer.py`
- `wechat_driver/windows_ui.py`

验收：

- 候选回复可人工确认。
- 确认后发送到正确会话。

### Step 8：工具运行时和 fake 工具

文件：

- `tools/base.py`
- `tools/registry.py`
- `tools/runtime.py`
- `tools/permissions.py`
- `tools/document/translator.py`
- `tools/search/external_search.py`

验收：

- 能注册工具。
- 能校验工具参数。
- 未授权路径被拒绝。
- fake 文档翻译工具完成 C07。
- fake 外网检索工具完成 C08。

### Step 9：真实文档解析与翻译

文件：

- `tools/document/parser.py`
- `tools/document/chunker.py`
- `tools/document/glossary.py`
- `tools/document/translator.py`
- `tools/document/docx_writer.py`
- `tools/document/math_renderer.py`

验收：

- 支持 PDF 和 DOCX。
- 长文档可分块。
- 翻译输出写入 DOCX。
- LaTeX/数学公式在 DOCX 中正确渲染。
- 失败 chunk 可重试。

### Step 10：真实浏览器自动化外网检索 provider

文件：

- `tools/search/provider_base.py`
- `tools/search/browser_provider.py`
- `tools/search/query_planner.py`
- `tools/search/result_filter.py`
- `tools/search/model_relevance_filter.py`
- `tools/search/result_translator.py`

验收：

- 接入你确认的浏览器和搜索入口。
- 默认避开国内词条和广告垃圾。
- 模型过滤层能过滤无关内容。
- 检索结果可翻译为中文摘要。
- 摘要包含来源 URL。
- 全内容存本地。

## 11. 每次开发后的汇报模板

每次开发或 debug 后，必须按这个格式汇报：

```text
修改位置：
- path/to/file.py：新增/修改了什么模块或函数。

修改内容：
- 做了什么。
- 为什么这么做。

验证方式：
- 运行了哪些命令或测试。

结果：
- 通过/失败/未执行。

仍需确认：
- 需要你 grill 的问题。
```

## 12. 当前不应开始实现的部分

以下部分在你回答对应问题前不应直接写真实实现：

- `WindowsUIWeChatDriver` 的真实 UI 操作。
- 真实微信发送。
- 群聊 `auto` 自动发送。
- 人设后台和每日轨迹自动生成。
- 你正式 topic 文件的实机替换。

可以先实现：

- 配置 schema。
- 配置 CLI/向导。
- domain models。
- fake driver。
- fake LLM。
- normalizer。
- router。
- AI topic classifier 临时模板。
- dry-run reply gate。
- JSONL logging。
- file index。
- replay harness。
- tool registry。
- tool runtime。
- fake document translate tool。
- fake external search tool。
- fake model relevance filter。
