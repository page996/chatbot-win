# WeFlow 本地消息抓取项目进度与下一阶段计划

日期：2026-07-01

## 当前完成度

总体完成度：约 75%。

已经完成的是本地抓取链路的工程骨架、安全边界、文件解析策略、sidebar 控制入口和自动化测试；尚未完成的是连接真实前台微信/WeFlow fork 的实机端到端验证，以及对撤回、删除、引用增强和长期后台稳定性观测。

## 当前状态

- sidebar 已启动：`http://127.0.0.1:8765`
- sidebar WeFlow 状态接口正常：`GET /api/weflow/status`
- WeFlow 主入口已固定为本地 fork：`http://127.0.0.1:5031`
- 正式拉取要求：
  - 必须本地 HTTP
  - 必须提供 token
  - 必须通过 `buildFlavor=chatbot-win-local-fork`
- WeFlow 后台 worker 当前未运行，需要在 sidebar 中手动启动或拉取一次。
- 当前依赖检查通过：
  - PyMuPDF / pypdf / pdfminer.six / openpyxl 可用
  - RapidOCR 走 `vendor/ocr-python/Scripts/python.exe`，可用
- `data/config.json` 已完成文件扩展迁移。

## 已完成能力

### 1. WeFlow 抓取主链路

- 建立 `WeFlowHttpBridge`。
- 支持 `/api/v1/messages` 原始消息拉取。
- 支持 talker 级别串行锁，多个 talker 可并发拉取。
- 支持按 `sortSeq -> createTime -> localId -> serverId -> messageKey` 排序。
- 支持去重状态和游标状态写入 `data/weflow_bridge_state.json`。
- 支持消息导入：
  - `hook_events.jsonl`
  - `backend_events.jsonl`
  - `conversation_ledgers`

### 2. 文件与多媒体链路

- 文本、DOCX、PDF、CSV、XLSX、XLSM 进入解析链路。
- PDF 改为全页渲染 OCR，不再限制前 20 页。
- OCR 结果写入 `media/ocr/index.md`，并暴露到对话上下文。
- 长文本 chunk 提升到约 4000 token。
- 图片/表情先 OCR；无有效 OCR 文本时写入可见占位符。
- 音频保留 ASR 链路；ASR 不可用时保留本地 artifact 和占位状态。
- PPT/PPTX、视频、压缩包、可执行文件只登记占位，不读取、不解压、不执行。

### 3. sidebar 集成

新增 WeFlow 面板：

- Health
- 拉取一次
- 后台启动
- 停止
- 依赖检查
- 依赖安装

对应本地接口：

- `GET /api/weflow/status`
- `POST /api/weflow/health`
- `POST /api/weflow/pull-once`
- `POST /api/weflow/start`
- `POST /api/weflow/stop`
- `POST /api/weflow/dependencies`
- `POST /api/weflow/install-deps`

### 4. 安全边界

- Python bridge 默认拒绝非本地 WeFlow 地址。
- 正式拉取强制 token。
- 正式拉取强制 fork marker。
- sidebar 不落盘 token，只记录 `token_present`。
- WCF/WeChatFerry 不是主执行器。
- 远程网络能力暂不作为正式抓取路径启用。

### 5. 测试状态

最后一次完整测试：

```text
python -m unittest discover -s tests
315 tests OK, skipped=1
```

## 未完成事项

### P0：真实 WeFlow/微信端到端验证

当前代码链路已完成，但还没有对真实前台微信执行完整拉取。

需要验证：

- WeFlow fork 是否成功注入/读取当前微信版本。
- `/api/v1/health` 是否返回 `buildFlavor=chatbot-win-local-fork`。
- token 是否能通过 sidebar 传入或从 `WEFLOW_API_TOKEN` 环境变量读取。
- `pull-once` 是否能真实写入：
  - `data/hook_events.jsonl`
  - `data/backend_events.jsonl`
  - `data/conversation_ledgers/.../conversation.md`
  - `data/file_workspace/...`

### P0：真实文件读取验证

需要用真实微信对话中已有文件验证：

- 文本
- 图片/表情
- 语音
- PDF 全页 OCR
- CSV
- PPT/PPTX 占位
- 视频占位
- 压缩包占位
- EXE/应用占位

重点检查 agent 在对话文件中是否能读到解析结果或占位符，而不是只看到文件名。

### P1：后台稳定性观察

需要连续运行 WeFlow 后台 worker，观察：

- 多 talker 并发是否无混抓。
- 单 talker 顺序是否稳定。
- 重复拉取是否正确去重。
- 大文件和大 PDF 是否不会阻塞整体 worker。
- `data/weflow_bridge_state.json` 是否持续正确推进。

### P1：引用/撤回/删除增强

当前已有 quote/recall 基础字段，但真实微信样本还不足。

后续补强：

- 引用消息定位到 ledger entry。
- 撤回消息标记原消息状态。
- 删除消息事件识别。
- 被引用附件上下文回填。

### P2：后台推送/SSE 增量模式

当前以 pull 为主，SSE 已有桥接基础。

后续方向：

- 验证 `/api/v1/push/messages`。
- 与 pull 状态去重合并。
- 断线重连和 Last-Event-ID 恢复。

## 下一阶段执行计划

### 阶段 1：实机连接准备

目标：确认本地 fork 与 token 可用。

步骤：

1. 启动 WeFlow fork。
2. 设置 `WEFLOW_API_TOKEN`，或在 sidebar WeFlow 面板手动填 token。
3. 打开 `http://127.0.0.1:8765`。
4. 在 WeFlow 面板点击 Health。
5. 确认返回：
   - `status=ok`
   - `fork_ok=true`
   - `buildFlavor=chatbot-win-local-fork`

验收标准：

- Health 通过。
- 没有连接到非本地地址。
- 没有 token 落盘。

### 阶段 2：单次真实拉取

目标：验证真实消息能进入对话文件。

步骤：

1. 在 WeFlow 面板填写 talker，或留空读取会话列表。
2. 设置 `workers=1`，先降低变量。
3. 点击“拉取一次”。
4. 检查 `hook_events.jsonl`、`backend_events.jsonl`、conversation ledger。
5. 让 agent 读取并复述本轮抓到的真实内容。

验收标准：

- 消息顺序正确。
- 自己和对方消息都被记录。
- 附件进入 file workspace。
- 对话文件包含文本块、artifact 路径、文件占位符或 OCR/ASR 内容。

### 阶段 3：多会话并发验证

目标：证明多并发不混抓。

步骤：

1. 选择至少 2 个 talker。
2. 设置 `workers=2` 或更高。
3. 连续执行多次 pull-once。
4. 对比每个 conversation ledger。

验收标准：

- 不同 talker 写入不同 conversation 文件。
- 每个 conversation 内 sequence 单调递增。
- 没有跨会话附件串入。
- 重复拉取不会重复写入旧消息。

### 阶段 4：后台运行

目标：让 sidebar 成为正式启动入口。

步骤：

1. 在 sidebar 点击“后台启动”。
2. 保持 WeFlow 和微信运行。
3. 观察 worker loops、last_status、last_error。
4. 发送新文本、图片、语音、文件，检查自动更新。

验收标准：

- worker 持续运行无异常。
- 新消息自动进入对应对话文件。
- 文件解析异步但最终可达。
- 停止按钮可停止 worker。

## 当前风险

- 真实微信版本适配仍需实机确认。
- WeFlow fork 需要实际启动并确认 health marker 已编译进运行版本。
- OCR/ASR 对超大 PDF 或长音频可能耗时，需要后续加任务队列和进度状态。
- 当前远程能力被默认降级，本阶段不建议开启非本地地址。

## 建议下一步

下一步先做 P0 实机连接：启动 WeFlow fork，在 sidebar 点击 Health。Health 通过后，再做一次受控的 `pull-once`，用一段包含文本、图片、PDF、CSV、语音的真实聊天记录验证完整链路。
