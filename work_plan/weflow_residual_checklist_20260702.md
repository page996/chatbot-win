# WeFlow 本地部署计划残留清单

日期：2026-07-02
分支：`weflow-stability-observability`

本清单在完成「会话隔离 / 文件-对话匹配 / 顺序保证 / 并发通路」深度审查与修复后产出，供审核与下一步规划。目标：稳定的本地部署并提供服务。

---

## 本轮已解决（你最关注的事项）

### ✅ R1. 会话身份不一致 / 重名混抓（严重，已修复）

- **问题**：driver 层用 `chat_title`（显示名）算 `conversation_id`，normalizer 层用 `conversation_key`（wxid/roomid）。真实 WeFlow 通路里两者不同 → 附件落盘目录与账本目录错位；且两个同显示名的联系人会被合并成同一会话，造成文件混抓、顺序错乱。
- **修复**：抽出共享 `_conversation_key` helper（driver + normalizer 同源），回退顺序 `conversation_key → talker_id → talker → chat_title`。driver 注入规范化 `conversation_key` 供 normalizer 读取。
- **验证**：新增 2 条回归测试（跨层一致、同名不同 talker 隔离）；复现脚本确认修复前后行为。commit `ca84c2c`。

### ✅ R2. 后台 worker 每 tick 重建 runtime（已修复，上一轮）

- 见 `weflow_background_observability_20260702.md`。commit `1911aa4`。

### ✅ 已确认正确、无需改动

- **抓取入口并发**：跨 talker `ThreadPoolExecutor` 并发 + 同 talker 文件锁串行；全局 state 单独锁合并。
- **调度并发隔离**：`ConversationScheduler` 同会话串行、跨会话并发。
- **文件中间层隔离**：`FileWorkspace` 按 `conversation_id/session_id/file_id` 目录隔离，跨会话天然不冲突。
- **driver 线程安全**：`read_new_messages` 单线程去重，`enrich_message_attachments` 多线程但无共享可变状态。
- **会话内顺序**：`_weflow_sort_messages` 按 `sortSeq→createTime→localId→serverId→messageKey` 稳定排序；ledger `sequence` per-conversation 单调递增；持久 `Deduper` 防重复回复。
- **文件类型安全边界**：ppt/archive/app/video/exe 仅占位登记，不读取/解压/执行。

---

## 残留事项（按优先级，待审核后规划）

### P0 — 必须实机验证（代码地基已就绪，无法在无 WeFlow 环境完成）

- **E1. 真实端到端拉取验证**：启动 WeFlow fork，Health 通过后做受控 `pull-once`，用含文本/图片/PDF/CSV/语音的真实会话验证完整链路。验收点：消息顺序正确、双方消息都记录、附件进 file_workspace、账本含解析/占位内容。
- **E2. 多会话并发实机验证**：≥2 talker、`workers≥2`、连续多次 pull，对比各 conversation ledger，确认不混抓、各自 sequence 单调、重复拉取正确去重。（R1 修复后逻辑上已保证，需实机确认。）

### P1 — 并发健壮性加固（本地部署稳定性关键）

- **C1. hook importer 单实例约束未强制**：两个 importer 同时消费同一 `hook_events.jsonl` 会争 state offset。当前靠约定（文档说"消费端保持单实例"），无运行时保护。建议：给 importer 加进程级锁文件（类似 talker lock），或在 sidebar worker 启动时检测并拒绝重复启动。
- **C2. file_index.sqlite 高并发偶发 `database is locked`**：全局 sqlite 短事务，多会话并发 `add()` 时可能偶发锁超时。建议：加 `PRAGMA busy_timeout` 或 WAL 模式，或加重试。低频但高并发下需处理。
- **C3. 后台 worker 异常退出无自动重启**：worker 线程若因未捕获异常终止（非 tick 内异常），sidebar 只显示 `running=false`，无自动拉起。建议：evaluate 是否需要 supervisor 或健康自愈。（当前 tick 内异常已被 catch 并重建 context，仅线程级崩溃未覆盖。）

### P2 — 功能完整性（计划已列，样本不足）

- **F1. 引用/撤回/删除增强**：quote 定位到 ledger entry、撤回标记原消息、被引用附件上下文回填。需真实微信样本。
- **F2. SSE 增量模式整合**：`listen-weflow-sse` 与 pull 状态去重合并、断线重连、Last-Event-ID 恢复接入后台 worker。
- **F3. 大文件/长音频不阻塞 worker**：超大 PDF 全页 OCR、长音频 ASR 可能阻塞单个 tick（已有 slow-tick 观测，但无异步任务队列）。建议：附件解析下沉到独立任务队列 + 进度状态。

### P3 — 部署与服务化

- **D1. 服务化启动**：sidebar 作为正式启动入口的稳定性（开机自启、崩溃恢复、日志轮转）。
- **D2. 配置与密钥管理**：`WEFLOW_API_TOKEN` 及 LLM key 的本地安全存储；sidebar 不落盘 token 已做，需覆盖 LLM key 路径。
- **D3. 资源占用观测**：长时间运行的内存/磁盘增长（file_workspace 累积、ledger 增长）监控与清理策略。

---

## 建议的审核决策点

1. **R1 修复是否需要迁移旧数据**：已有用 chat_title 算的 conversation_id 的历史账本/文件目录，在修复后会与新 id 不匹配。需决定是否写迁移脚本，还是视为历史数据保留。（实机部署前若无生产数据，可忽略。）
2. **P1 三项加固优先级**：C1（importer 单实例）风险最直接，建议优先。
3. **下一步**：建议先做 E1/E2 实机验证（需你启动 WeFlow），或先做 P1 加固（我可在无 WeFlow 环境完成 + 测试）。

---

## 当前测试状态

`python -m unittest discover -s tests` → **327 OK, skipped=1**。
