# 微信对话文件工作流探测记录

日期：2026-07-01

## 当前环境

- 微信版本：4.1.10.53
- 微信文件存储目录：`E:\aab通讯文件存储\xwechat_files`
- 本地 WeFlow HTTP：未启动，`http://127.0.0.1:5031/api/v1/health` 连接被拒绝
- 已绑定发送窗口：`PAGE`，conversation_id=`5099a4249ac4bc9915f30694`
- 当前真实发送驱动：`windows_guarded`

## 探测结论

当前不依赖前端焦点时，可以读到微信文件缓存和文件目录，但不能完整还原文本/语音消息历史顺序：

- `db_storage/*.db` 不是普通 SQLite，直接只读打开报 `file is not a database`。
- WeFlow 未启动时，无法通过本地 HTTP/SSE 获取底层消息队列和历史文本。
- 本地文件缓存可读，适合先跑通“对话文件进入 agent 工作流”。

按文件头探测到的主要类型：

- PDF：可读，样本 `Checklist.pdf`
- CSV/表格：可读，样本 `cross_border_cost_time_distribution_seed.csv`
- JPG/PNG：可读，JPG 已 OCR 成功；部分无扩展 HTTPResource 是 PNG/JPEG
- Office：存在 DOCX/XLSX/PPTX，当前默认支持 DOCX/XLSX
- 视频：存在 MP4，但当前默认不解析
- 微信图片 `.dat`：大量存在，其中部分可能是图片缓存/缩略图，但不在默认白名单内
- 语音：未发现普通 `.amr/.silk/.m4a/.aac/.mp3/.wav` 可直接读取缓存；`wechat-voice-cache-probe` 因缺少语音消息线索而 blocked

默认配置边界：

- `file_read_roots`: `["inbox"]`
- 本轮通过命令临时使用 `--extra-root E:\aab通讯文件存储\xwechat_files`
- `file_max_bytes`: 20 MB
- 默认支持扩展：`.txt`, `.md`, `.docx`, `.pdf`, `.csv`, `.xlsm`, `.xlsx`, `.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp`

## 已跑通的真实文件样本

三条事件写入临时文件：`data\probe_backend_events.jsonl`，并通过 `poll-backend-events --extra-root E:\aab通讯文件存储\xwechat_files` 进入 PAGE 会话账本。

已入账到：`data\conversation_ledgers\5099a4249ac4bc9915f30694\messages.jsonl`

样本结果：

- `Checklist.pdf`
  - file_id: `1a2912c3ac052fcac9dd6d8b`
  - status: `indexed`
  - parse: `parsed/pdf`
  - content: `data\file_workspace\5099a4249ac4bc9915f30694\session_default\1a2912c3ac052fcac9dd6d8b\derived\content.md`
- `cross_border_cost_time_distribution_seed.csv`
  - file_id: `857d05fd4da1447f5912f82c`
  - status: `indexed`
  - parse: `parsed/spreadsheet`
  - content: `data\file_workspace\5099a4249ac4bc9915f30694\session_default\857d05fd4da1447f5912f82c\derived\content.md`
- `586_1782714783_thumb.jpg`
  - file_id: `c0182d38119e1894d31d58b0`
  - status: `indexed`
  - parse: `parsed/image`
  - content: `data\file_workspace\5099a4249ac4bc9915f30694\session_default\c0182d38119e1894d31d58b0\derived\content.md`

## 真实发送状态

当前不能由 agent 在后台安全地强制切换微信会话并发送。`windows_guarded` 的设计是：

- 只允许在当前前台窗口看起来是微信时发送。
- 只允许目标会话标题匹配，或前台窗口与手动绑定窗口匹配时发送。
- 发送方式是粘贴文本并回车。

已做适配：

- 微信 4.1.10.53 主窗口标题可能只有“微信”，不会包含 `PAGE`。
- 已增强 `windows_guarded`：如果前台 hwnd 与手动绑定的 PAGE 窗口一致，也允许发送。

真实发送前需要手动操作：

1. 在微信中打开 `PAGE` 聊天。
2. 将该微信聊天窗口置于前台，输入框可用。
3. 回到本线程回复“已切到 PAGE”。
4. agent 先运行 `send-driver-probe`，确认 ready 后再发送短测试消息。

## 后续建议

- 要获取完整文本、语音、引用、撤回和删除消息历史，优先启动 WeFlow 本地 HTTP/SSE，再用本项目的 WeFlow bridge 拉取。
- 语音历史如果没有可读音频缓存，需要 WeFlow/WCF 提供语音元数据，或通过前端选中语音气泡走微信内置转文字。
- `.dat` 图片支持可以后续做专门识别/转码适配，不建议直接把所有 `.dat` 加入默认白名单。
