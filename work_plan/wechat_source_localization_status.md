# 微信消息源本地化与外部项目审计

更新日期：2026-07-01

## 当前结论

本项目已经实现“本地消息源适配器 -> hook_events.jsonl -> backend_events.jsonl -> 对话账本”的自动拉取链路，但没有把 DLL 注入、偏移定位、微信数据库解密等强版本耦合逻辑并入主工程。底层消息捕获仍建议交给 WeFlow 或 WeChatFerry 这类外部本地服务，本项目只连接 localhost 并消费结构化消息。

默认建议：

- 微信 4.1.10.53 优先用 WeFlow 的本地 HTTP/SSE。
- WeChatFerry 只作为可选后备，不默认启用它的 HTTP 服务。
- 外部项目全部放在 `vendor/reference/` 做本地参考，不纳入主工程运行时依赖。
- 所有采集默认只连 `127.0.0.1`、`localhost`、`::1`；除非显式 `--allow-non-local`。

## 本地参考仓库

- WeFlow：`vendor/reference/WeFlow-gitcode`
  - Remote: `https://gitcode.com/u012416915/WeFlow.git`
  - Commit: `93d46a3183b98bfe5073ea89ecaa32a4f80f79d3`
  - Last commit: `2026-05-29 20:44:44 +0800 Merge pull request #1041 from hicccc77/dev`
- WeChatFerry：`vendor/reference/WeChatFerry-gitee`
  - Remote: `https://gitee.com/bmyx/WeChatFerry.git`
  - Commit: `7053215e4933f55846631971e61de91cd4a729e8`
  - Last commit: `2023-05-12 11:54:47 +0800 Update README`

## 本项目新增/参与的工作

本项目参与的是“消息接收与写出器”，不参与微信进程注入和消息发送：

```text
WeFlow localhost HTTP/SSE
  或 WeChatFerry localhost callback
  -> data/hook_events.jsonl
  -> import-hook-events / pull-hook-messages
  -> data/backend_events.jsonl
  -> BackendEventJsonlDriver
  -> PollingRunner
  -> conversation_ledgers
```

已接入命令：

- `append-hook-source-event`：把单条外部源 JSON 归一化写入 `hook_events.jsonl`。
- `weflow-health`：检查 WeFlow 本地 HTTP API。
- `pull-weflow-messages`：轮询 WeFlow 会话消息，写入 hook JSONL，并立即导入处理。
- `listen-weflow-sse`：监听 WeFlow SSE 推送，保存 `Last-Event-ID`，跳过 ready 事件，去重写入 hook JSONL。
- `wcf-callback-sink`：启动本项目的 localhost-only WCF 回调接收器，仅写入 hook JSONL，不暴露发送能力。
- `pull-hook-messages`：持续消费 `hook_events.jsonl`，导入并处理。

新增关键文件：

- `app/personal_wechat_bot/wechat_driver/hook_source_bridge.py`
- `app/personal_wechat_bot/runtime/hook_pull_runner.py`
- `tests/test_hook_source_bridge.py`

## WeFlow 本地暴露接口

源码依据：`vendor/reference/WeFlow-gitcode/electron/services/httpService.ts`

默认配置依据：`vendor/reference/WeFlow-gitcode/electron/services/config.ts`

- 默认 HTTP API：`httpApiHost = 127.0.0.1`，`httpApiPort = 5031`，`httpApiEnabled = false`
- 默认消息推送：`messagePushEnabled = false`
- 鉴权：`/health`、`/api/v1/health` 不需要 token；`/api/v1/*` 需要 token，支持 Bearer、query `access_token`、POST body。

本地 HTTP API 路由：

- `GET|POST /health`
- `GET|POST /api/v1/health`
- `GET|POST /api/v1/push/messages`
- `GET|POST /api/v1/messages`
- `GET|POST /api/v1/sessions`
- `GET /api/v1/sessions/:id/messages`
- `GET|POST /api/v1/contacts`
- `GET|POST /api/v1/group-members`
- `GET /api/v1/sns/timeline`
- `GET /api/v1/sns/usernames`
- `GET /api/v1/sns/export/stats`
- `GET /api/v1/sns/media/proxy`
- `POST /api/v1/sns/export`
- `GET /api/v1/sns/block-delete/status`
- `POST /api/v1/sns/block-delete/install`
- `POST /api/v1/sns/block-delete/uninstall`
- `DELETE /api/v1/sns/post/{postId}`
- `GET /api/v1/media/*`

本项目实际只使用：

- `GET /api/v1/health`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/:id/messages`
- `GET /api/v1/push/messages`

本项目不调用：

- SNS 导出、媒体代理、防删除、删除朋友圈、本地媒体服务等接口。
- WeFlow 内部 IPC 的 `chat:updateMessage`、`chat:deleteMessage`。

## WeFlow 远程网络与风险

未在 WeFlow HTTP API 中看到发送微信聊天消息的路由；从本地扫描看，它更像“读本地消息库/导出/分析”。但 WeFlow 仍有外部网络能力：

- 自动更新：`electron/main.ts` 使用 `electron-updater`，feed 指向 GitHub releases；启动时会检查更新，下载需要用户触发。
- AI/LLM：`groupSummaryService.ts`、`insightService.ts`、`insightProfileService.ts` 会向配置的 `/chat/completions` 发 HTTP/HTTPS 请求，并带 `Authorization: Bearer ...`。
- 朋友圈媒体：`snsService.ts` 对微信 CDN 图片/视频 URL 使用 `https.request` 下载或代理。
- 云统计：`cloudControlService.ts` 会构造 appVersion、platform、deviceId、pages 等 usage stats，并调用 `wcdbService.cloudReport()`；最终网络行为在 native WCDB DLL 内，无法仅从 TS 完整审计。

建议运行姿态：

- 只开启 `httpApiEnabled` 和必要的 `messagePushEnabled`。
- `httpApiHost` 固定为 `127.0.0.1`。
- 不配置 AI API Key、Telegram、Weibo Cookie 等远程功能。
- 如需严格本地-only，用 Windows 防火墙阻断 WeFlow 进程出站，只允许本机回环访问。

## WeChatFerry 本地暴露接口

源码依据：

- Python SDK：`vendor/reference/WeChatFerry-gitee/python/wcferry/client.py`
- HTTP 包装：`vendor/reference/WeChatFerry-gitee/http/wcfhttp/main.py`
- HTTP 路由：`vendor/reference/WeChatFerry-gitee/http/wcfhttp/core.py`
- RPC server：`vendor/reference/WeChatFerry-gitee/spy/rpc_server.cpp`

Python SDK：

- `Wcf(host=None, port=10086)` 默认本地启动并连接 `127.0.0.1:10086`。
- 接收消息占用 `port + 1`，即默认 `10087`。
- `enable_receiving_msg()` / `get_msg()` 可本地读取消息队列。
- SDK 也包含发送能力：`send_text`、`send_image`、`send_file` 等。本项目当前不调用这些发送 API。

底层 RPC：

- WeChatFerry README 和 `spy/rpc_server.cpp` 显示注入侧 RPC 默认可能监听 `tcp://0.0.0.0:10086` 和 `10087`。
- 这意味着如果防火墙未限制，局域网内其他主机可能尝试连接 RPC 端口。

WCF HTTP 包装默认：

- `--host 0.0.0.0`
- `--port 9999`
- `--cb` 可把收到的每条消息 `requests.post(cb, json=data)` 转发出去。

WCF HTTP 路由包括：

- 只读/状态：`GET /login`、`GET /wxid`、`GET /user-info`、`GET /msg-types`、`GET /contacts`、`GET /friends`、`GET /dbs`、`GET /{db}/tables`
- 示例回调：`POST /msg_cb`
- 发送消息：`POST /text`、`POST /image`、`POST /file`、`POST /xml`、`POST /emotion`
- 敏感操作：`POST /sql`、`POST /new-friend`、`POST /chatroom-member`、`POST /transfer`、`POST /dec-image`

## WeChatFerry 远程网络与风险

已确认的风险点：

- WCF HTTP 默认监听 `0.0.0.0:9999`，且没有在源码中看到默认认证层。
- WCF HTTP 暴露发送消息、执行 SQL、收款、加群、通过好友等接口。
- `--cb` 如果配置成远程 URL，会把每条收到的微信消息 POST 到远端。
- 注入侧 RPC 默认可能监听 `0.0.0.0:10086/10087`。

本项目默认不启动 WCF HTTP。若必须使用，建议：

```powershell
wcfhttp --host 127.0.0.1 --port 9999 --cb http://127.0.0.1:8791/callback
```

并用防火墙限制：

- 只允许 `127.0.0.1` 访问 `9999`、`10086`、`10087`。
- 不把 `--cb` 指向非本机地址。
- 不把 WCF HTTP 作为公网或局域网服务暴露。

## 当前可用命令

WeFlow 健康检查：

```powershell
python -m app.personal_wechat_bot.main --data-dir data weflow-health `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN
```

WeFlow 轮询并进入处理链路：

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-weflow-messages `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN `
  --hook-event-file data/hook_events.jsonl `
  --backend-event-file data/backend_events.jsonl `
  --forever `
  --interval 1
```

WeFlow SSE 监听，只写 hook JSONL：

```powershell
python -m app.personal_wechat_bot.main --data-dir data listen-weflow-sse `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN `
  --hook-event-file data/hook_events.jsonl `
  --weflow-state-file data/weflow_bridge_state.json `
  --forever
```

单独消费 hook JSONL：

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-hook-messages `
  --hook-event-file data/hook_events.jsonl `
  --backend-event-file data/backend_events.jsonl `
  --forever `
  --interval 1
```

WCF 本机 callback sink：

```powershell
python -m app.personal_wechat_bot.main --data-dir data wcf-callback-sink `
  --host 127.0.0.1 `
  --port 8791 `
  --path /callback `
  --hook-event-file data/hook_events.jsonl
```

## 后门判断

从当前本地源码扫描看：

- 没有证明 WeFlow 或 WeChatFerry 存在隐藏远程控制后门。
- WeFlow 存在明确的远程网络功能：更新、AI 请求、朋友圈媒体下载、云统计 native 调用。
- WeChatFerry HTTP 存在明确的远程暴露风险：默认 `0.0.0.0`、无默认认证、含发送/SQL/好友/转账等敏感接口、callback 可远程转发消息。
- 因此风险不是“已发现隐藏后门”，而是“外部服务能力过大，必须本地绑定和防火墙隔离”。

本项目自身的桥接器：

- 默认拒绝非本机 WeFlow URL。
- WCF callback sink 默认只绑定 `127.0.0.1`，且拒绝非 localhost 绑定。
- 所有输出只写本地 JSONL，不实现微信发送接口。
- 下游 `BackendEventJsonlDriver.send_message()` 固定返回 failed，不会发送微信消息。

## 验证

已新增测试：

- WeFlow 非本机 URL 默认拒绝。
- WeFlow pull 消息附件/引用归一化。
- WeFlow recall 事件能指向原消息 `target_raw_id`。
- WCF callback 群消息和媒体归一化。
- WeFlow SSE 跳过 ready、保存 Last-Event-ID、去重写入。

建议最终验证：

```powershell
python -m unittest tests.test_hook_source_bridge tests.test_hook_events tests.test_backend_events_cli
python -m unittest discover -s tests
```

## 2026-07-01 追问答复与当前能力边界

### 1. 消息源定位、排序、高并发与自动抓取

当前链路不依赖前端焦点锁定。WeFlow HTTP 轮询按 session/talker 拉取，WeFlow SSE 按推送事件写入，WCF callback sink 接收本机回调；三者都写入本地 `hook_events.jsonl`，再由单个 importer/runner 消费到 `backend_events.jsonl` 和账本。

消息源定位字段已经保留到下游：

- `conversation_key` / `talker_id`：会话源，通常是 wxid 或 `@chatroom`。
- `sender_wechat_id` / `sender_name`：发送者。
- `source` / `adapter`：`weflow_http`、`weflow_push`、`wechatferry_callback` 等采集来源。
- `msg_id` / `server_id` / `local_id` / `sort_key`：底层消息标识和排序线索。
- `source_path` / `source_line_no` / `source_offset` / `batch_index` / `import_sequence`：本地 JSONL 源定位和导入顺序。
- `ordering`：下游 metadata 中的稳定排序摘要。

排序策略现状：

- 消费顺序以 JSONL 写入顺序为准，适合实时流水线。
- 同一会话内可用 `(observed_at, sort_key/server_id/local_id/msg_id, source_offset, batch_index, import_sequence)` 做稳定复放或迁移排序。
- 当前不会在已处理队列中全局重排，否则会改变已生成回复和账本顺序。

并发能力现状：

- 多个采集源可以并发写同一个 JSONL，总线写入已加本地 `.lock` 文件，降低行交错风险。
- 消费端建议保持单实例；两个 importer 同时消费同一个 hook 文件仍可能竞争 state offset。
- 高并发多对话主要依赖 WeFlow/WCF 底层事件完整性，本项目负责去重、会话隔离和顺序元数据保留。

### 2. 历史对话可见性与迁移

实时触发拉取默认只处理新消息或 lookback 窗口内消息。历史对话是否可见取决于外部本地源能否返回历史：

- WeFlow：可通过 `GET /api/v1/sessions/:id/messages` 拉取某个 session 的历史窗口；本项目命令 `pull-weflow-messages` 已支持 `--talker`、`--since`、`--message-limit`。
- WCF：可通过 SDK/数据库能力读历史，但本项目默认没有启用 WCF SQL/DB 导出，因为接口权限太大。

迁移建议：

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-weflow-messages `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN `
  --talker wxid_or_roomid `
  --since 0 `
  --message-limit 1000 `
  --hook-event-file data/hook_events_history.jsonl `
  --backend-event-file data/backend_events_history.jsonl `
  --loops 1 `
  --extra-root data/inbox
```

如果 WeFlow 对单次 limit 或时间范围有限制，需要分 talker、分时间窗多次跑。导入后的历史会进入同一账本格式，但建议先用独立 `*_history.jsonl` 验证后再合并到生产数据目录。

### 3. 文件类型、双方记录与消息方向

双方消息记录：

- 外部采集源提供 `is_self` / `isSend` 时，本项目会记录“自己”和“对方”的消息。
- `is_self=True` 的微信消息会进入账本，role 为 `self`，但不会触发 agent 回复。
- agent 生成的回复会进入账本，role 为 `assistant`。

文件类型边界：

- 当前不是“所有微信文件类型都自动下载并解析”。
- 只有外部源给出本地路径或可访问路径时，附件才会进入 `AttachmentPipeline`。
- 默认解析受 `file_read_roots`、扩展名白名单和 `file_max_bytes` 限制。
- 图片、PDF、Office、文本、音频等能否解析取决于本地 parser/ASR/OCR 能力。
- WeFlow 的 `/api/v1/media/*`、SNS 媒体代理、WCF 解密图片/文件接口暂未纳入默认流程，避免过早扩大网络和敏感接口面。

### 4. Agent 自己发送的文件或消息

已强化本地账本：

- `ReplyCandidate` 现在支持 `attachments` 与 `send_metadata`。
- `append_reply()` 会把 agent 回复文本、显式附件、工具输出 `tool_result.output_refs` 记录为 outgoing attachments。
- 发送结果会回写到同一条 assistant ledger entry 的 `send` 字段，例如 `skipped`、`queued_for_confirm`、`queued_to_bridge`、`sent`、`failed`。

仍未做的事：

- 当前发送执行器仍只发送文本，不自动调用 WeFlow/WCF 发送文件。
- outgoing attachment 先代表“本地生成/准备发送/工具输出文件”，不等于微信侧已经发出。
- 后续若启用文件发送，需要在发送成功回执中补写微信外部 message id、文件名、发送 driver、失败原因。

### 5. 通道稳定性与远程网络能力

当前通道足以先跑通“对话 + 文件工作流”的本地闭环：

- WeFlow/WCF 作为本地采集源。
- JSONL 总线作为可审计、可复放的本地边界。
- backend driver 进入 normalizer/router/ledger/file parser。
- 默认不把微信发送接口、SQL、远程 callback、云 AI/SNS 媒体代理纳入运行路径。

远程网络能力可以作为后续方向，但建议按顺序推进：

1. 先稳定本地消息和文件入账。
2. 再接 WeFlow 本地媒体导出/下载，仍限制 localhost 和本地文件落盘。
3. 再评估发送文本与发送文件能力，默认 confirm 模式。
4. 最后才考虑远程更新、云摘要、远程 LLM、外网媒体代理，并为每项能力加显式开关、审计日志、host allowlist 和防火墙建议。

当前推荐先跑的工作流：

```powershell
python -m app.personal_wechat_bot.main --data-dir data pull-weflow-messages `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN `
  --hook-event-file data/hook_events.jsonl `
  --backend-event-file data/backend_events.jsonl `
  --forever `
  --interval 1 `
  --extra-root data/inbox
```

若只想监听实时 push：

```powershell
python -m app.personal_wechat_bot.main --data-dir data listen-weflow-sse `
  --base-url http://127.0.0.1:5031 `
  --token YOUR_TOKEN `
  --hook-event-file data/hook_events.jsonl `
  --forever

python -m app.personal_wechat_bot.main --data-dir data pull-hook-messages `
  --hook-event-file data/hook_events.jsonl `
  --backend-event-file data/backend_events.jsonl `
  --forever `
  --interval 1 `
  --extra-root data/inbox
```
