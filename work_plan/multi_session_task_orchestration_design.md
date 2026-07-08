# 总台与通道状态管理器设计

schema: multi_session_task_orchestration_design_v2
status: archived_reference
superseded_by: 通道状态管理器推进计划.md

> 本文保留为此前总台与通道状态管理器的设计背景。当前执行入口见 `通道状态管理器推进计划.md`。

## 目标

- 总台只做资源与调度：GPU、LLM、WeChat IO、CPU/文件 IO、发送桥。
- 通道状态管理器只管单个会话：当前主题、历史主题、文件状态、待回复、暂停/等待点、优先级。
- 前端展示拆成三类，不再互相污染：
  - 任务队列：真实仍在排队、运行、暂停、等待、阻塞的工作。
  - 操作历史：用户点击、后台 tick、完成/失败事件的审计流。
  - Channel Lanes：只展示会话内主题/任务，不展示全局 WeFlow 拉取、依赖检查、设置保存等系统任务。
- 昂贵处理必须可去重：OCR、ASR、表格、AI Analysis、Key Points 由文件状态决定是否复用。
- 清空历史只能清内容与运行残留，不能破坏配置、发送审核机制、非前台桥机制、GPU/ASR/OCR 配置、模型配置、密钥池、技能/人设。

## 核心记录

### ResourcePool

- `pool_id`: `gpu | llm | wechat_io | cpu_io | send_bridge`
- `max_parallel`
- `active`
- `queued`
- `reserved_by`
- `last_error`

默认策略：

- `gpu`: 1。OCR/ASR 同时抢占会导致显存抖动，先单槽，后续可按显存探测扩容。
- `llm`: 2。由 key pool / provider limit 决定。
- `wechat_io`: 1。WeFlow 拉取、回填、hook consume 共享游标，必须串行。
- `cpu_io`: 2。PDF 渲染、表格抽取、文件复制。
- `send_bridge`: 1。发送队列由 bridge worker 顺序消费。

### TaskRecord

必备字段：

- `task_id`
- `conversation_id`
- `session_id`
- `topic_id`
- `kind`: `pull | backfill | file_parse | file_ai_analysis | reply | send | user_topic | maintenance`
- `status`: `queued | running | waiting | paused | blocked | completed | failed | cancelled`
- `priority`: 0-100
- `resource_class`
- `concurrency_key`
- `dependencies`
- `stop_and_wait`
- `external_id`
- `progress`
- `phase`
- `detail`
- `created_at / updated_at / started_at / finished_at`

### ChannelState

- `conversation_id`
- `current_topic`
- `topic_queue`
- `topic_history`
- `active_tasks`
- `waiting_tasks`
- `paused_tasks`
- `file_states`
- `reply_state`
- `last_user_message_at`
- `last_agent_reply_at`
- `resource_audit`

### FileDerivedState

- `file_id`
- `source_sha256`
- `size_bytes`
- `max_bytes_at_ingest`
- `parse_cache_version`
- `ocr_cache_version`
- `asr_cache_version`
- `analysis_cache_version`
- `engine_signature`
- `status`: `placeholder | staged | parsed | analyzed | skipped_too_large | failed`
- `content_path`
- `analysis_path`
- `chunks`
- `ai_summary`
- `ai_key_points`
- `last_model_visible_sync_at`

## 优先级算法

```text
priority_score =
  status_weight
  + user_priority * 2
  + interactive_bonus
  + conversation_recency_bonus
  + age_bonus
  - background_penalty
  - pause_penalty
  - dependency_penalty
  - resource_pressure_penalty
```

排序原则：

- 新用户输入不必然最高优先；只有当它是明确交互需求、纠错、取消、继续、发送确认时才提升。
- 后台拉取、历史回填、文件 AI 总结默认低于当前对话回复。
- 已进入 `running` 的任务不抢杀；只允许用户取消或进入 stop-and-wait。
- `waiting/blocked/paused` 释放资源槽。

## 前端投影

### 顶部资源监视器

展示：

- CPU、内存、GPU 显存/GPU 利用率（能拿到多少展示多少）
- GPU gate: `active/max_parallel`
- LLM gate: `active/max_parallel`
- WeChat IO: `idle/running/blocked`
- Send bridge: `idle/running/backlog`

### 任务栏

只展示真实任务，不展示单纯按钮点击残留。

- 活动任务：queued/running/waiting/paused/blocked
- 历史任务：completed/failed/cancelled
- 支持按资源池、会话、状态过滤。

### Channel Lanes

只展示 `conversation_id` 非空且属于会话主题的任务。

不进入 Channel Lanes：

- `weflow:*`
- `ui:*`
- `diagnostic:*`
- `settings:*`
- `audit:*`
- `history:*`
- `queue:*`
- `send-review:*`
- `channels:*`

### WeFlow 操作历史

仍留在 WeFlow 页，只做 WeFlow 源侧审计：

- health
- discover
- pull start/progress/done
- backfill start/progress/done
- worker tick
- cursor/seen/raw import 统计

不要和全局任务队列混成同一套 UI。

## 文件状态去重

文件处理的非重复规则：

- `source_sha256` 一致
- cache version 一致
- OCR/ASR engine signature 一致
- analyzer input signature 一致
- artifact 文件存在
- terminal success 状态存在

满足以上条件时不重新 OCR/ASR/AI 分析。

允许重跑：

- artifact 缺失或 JSON 损坏
- cache version 升级
- OCR/ASR 模式变更
- 上次失败且用户手动重试
- async pending 超过重试阈值
- 用户提高文件大小阈值后重新处理原先 `skipped_too_large` 文件

## 清空历史边界

清除：

- conversation ledgers/sessions/channels
- file workspace/index
- backend/hook/weflow 消息历史与游标
- task manager runtime history
- send audit/confirm queue/outbox 内容

保留：

- `config.json`
- 模型配置、密钥池
- 技能/人设/任务卡
- GPU/OCR/ASR 配置
- 文件大小阈值
- 白名单/黑名单/topic rules
- send driver/mode/enabled 配置

清理后必须重建空文件：

- `confirm_queue.jsonl`
- `send_audit.jsonl`
- `send_bridge/outbox.jsonl`
- `send_bridge/acks.jsonl`

## 下一步落地顺序

已完成第一版：

1. 当前补丁已收敛断链：context-only 泄漏、发送控制位置、桥/审核空文件重建、Channel Lanes 污染、WeFlow stale task 修复。
2. 后端已引入 `ResourceScheduler`，总台状态会展示前台/后台 LLM 预算、媒体/文件 I/O 建议与 GPU gate。
3. 已引入 `ChannelStateStore`，从 channel registry、scheduler task、ledger/file state 投影每个会话 lane。
4. 前端已拆为总台资源池、总台进程、通道状态管理器、独立操作历史与 WeFlow 操作历史。
5. 文件解析、异步文件 AI Analysis、回复生成、发送审核/发送桥已开始产生明确 `TaskRecord` 子任务。

仍需后续迁移：

1. 将更多长耗时工具调用、外发附件处理、批量回填子步骤也纳入 `TaskRecord`。
2. 让后台 worker 通过 `claim_next()` 主动领取可运行任务，而不是只把现有同步链路投影成状态。
3. 将 WeFlow 操作历史中的关键失败事件按需升格为总台任务，同时保持普通 tick 不污染主队列。
4. 给前端增加任务过滤与任务事件详情，直接读取 `/api/tasks` 的 event stream。
