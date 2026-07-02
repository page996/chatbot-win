# WeFlow 本地抓取链路与安全边界

更新时间：2026-07-01

## 主链路

当前主抓取路径只认 WeFlow 本地 fork：

1. WeFlow fork 从微信底层数据/媒体导出接口提供本地 HTTP API。
2. sidebar 或 CLI 调用 WeFlow `/api/v1/messages` 拉取消息。
3. Python bridge 写入 `data/hook_events.jsonl`。
4. `HookEventJsonlImporter` 导入 `data/backend_events.jsonl`。
5. `BackendEventJsonlDriver` 进入附件解析、OCR/ASR、文件中间层。
6. `ConversationLedgerStore` 顺序写入 `data/conversation_ledgers/<conversation>/conversation.md` 和 `messages.jsonl`。

WeChatFerry/WCF 当前不是主执行器，只保留为历史兼容/诊断入口。

## 本地 HTTP 接口

### sidebar，默认 `http://127.0.0.1:8765`

- `GET /api/weflow/status`：读取 WeFlow 面板状态，不触发抓取。
- `POST /api/weflow/health`：检查 WeFlow fork marker，不写 hook 文件。
- `POST /api/weflow/pull-once`：校验 token + fork marker 后拉取一次，并立刻导入/处理到对话文件。
- `POST /api/weflow/start`：启动本地后台循环拉取。
- `POST /api/weflow/stop`：停止本地后台循环拉取。
- `POST /api/weflow/dependencies`：检查 PDF/OCR/表格解析依赖。
- `POST /api/weflow/install-deps`：显式确认后执行 `pip install -r requirements-ocr.txt`。

### WeFlow fork，默认 `http://127.0.0.1:5031`

- `GET /health` 和 `GET /api/v1/health`：不需要 token，只暴露本地状态、`buildFlavor=chatbot-win-local-fork`、能力声明和 `mediaExportPath`。
- `GET /api/v1/messages`：需要 token；按 talker 拉取原始消息，支持媒体导出。
- `GET /api/v1/sessions`：需要 token；列出会话。
- `GET /api/v1/push/messages`：需要 token；SSE 增量事件。
- `GET /api/v1/media/*`：需要 token；只允许读取 WeFlow 媒体导出目录内文件。

## 本地文件写入

- `data/hook_events.jsonl`：WeFlow 原始标准化事件。
- `data/backend_events.jsonl`：导入后的后端消息事件。
- `data/weflow_bridge_state.json`：WeFlow 分 talker 游标、去重状态。
- `data/hook_events_state.json`：hook JSONL 导入 offset。
- `data/weflow_sidebar_state.json`：sidebar 最近 health/pull 状态；不写 token。
- `data/file_workspace/`：按 conversation/session/file_id 隔离保存原始文件、解析结果、chunk、OCR/ASR 产物。
- `data/conversation_ledgers/`：顺序对话文件和 JSONL ledger。

## 并发与顺序

- 跨 talker 可并发拉取，`workers` 控制并发数。
- 同一 talker 使用 `.talker-<hash>.lock` 串行化，避免同一会话翻页混写。
- 消息按 `sortSeq -> createTime -> localId -> serverId -> messageKey` 排序。
- 导入阶段记录 `import_sequence`，ledger 每个 conversation 有独立锁和 sequence。

## 文件解析策略

- 文本、DOCX、PDF、CSV/XLSX/XLSM：进入解析与 artifact/chunk 链路。
- PDF：渲染全部页面进入 OCR，不再只取前 20 页；OCR 结果写入 `media/ocr/index.md`。
- 图片/表情：先 OCR；无有效 OCR 文本时写入可见占位符。
- 音频：本地 ASR 可用则转写；不可用时保留音频 artifact 和占位状态。
- PPT/PPTX、视频、压缩包、可执行文件：只登记与占位，不读取、不解压、不执行。

## 远程与后门检查

- Python bridge 默认只允许 `http://127.0.0.1`、`localhost`、`::1`。
- 正式拉取必须有 token，且 WeFlow health 必须返回 `buildFlavor=chatbot-win-local-fork`。
- sidebar 状态文件只记录 `token_present`，不落盘 token。
- 本轮未加入任何远程发送接口；非本地 URL 需要显式 `allow_non_local` 才能通过 Python 检查，sidebar UI 默认不提供该开关。
- WeFlow fork 的非 health API 都经 `Authorization: Bearer <token>` 或 access token 校验。
