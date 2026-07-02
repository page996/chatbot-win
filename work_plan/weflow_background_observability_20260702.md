# WeFlow 后台稳定性观测地基

日期：2026-07-02

## 背景

`weflow_next_phase_plan_20260701.md` 的 P1「后台稳定性观察」要求在连续运行后台 worker 时能回答：多 talker 是否无混抓、单 talker 顺序是否稳定、重复拉取是否正确去重、大文件是否阻塞整体 worker、`weflow_bridge_state.json` 是否持续正确推进。原 worker 只记录 `loops / last_status / last_error / last_tick_at`，不足以回答这些问题。

## 本轮修复的 bug

`control/sidebar_api.py` 的 `_weflow_background_loop` 之前每个 tick 都调用 `_run_sidebar_weflow_once`，会重建整个 `build_runtime()` 和 `BackendEventJsonlDriver`。由于 driver 的内存去重集合（`_seen_event_ids` / `_seen_message_raw_ids`）每个 tick 都清空，`read_new_messages()` 会在每一轮重新读取并归一化整个 `backend_events.jsonl`。

- 持久 SQLite `Deduper` 保证不会重复回复，所以输出不错。
- 但会造成 O(N²) 增长、并对全部历史链接重复触发 `web.fetch`、对全部消息重复跑 memory maintenance。

修复：拆成 `_build_weflow_pull_context`（构建一次）+ `_run_weflow_pull_tick`（复用）。后台循环只在 tick 抛异常或 source 总失败（WeFlow 掉线）时重建 context，从而在 WeFlow 晚启动/重启后仍能自愈。

## 新增地基组件

### 1. `runtime/weflow_worker_metrics.py`

`WeflowWorkerMetrics`：

- 累计 `scanned / appended / imported / processed / errors` 总量。
- 有界 ring buffer 保存最近 N 个 tick（默认 50），每个 tick 带 `duration_seconds`，据此判定 `slow`（默认 ≥20s）→ 检测大文件阻塞。
- 记录 `last_success_at` / `last_progress_at`；`snapshot()` 计算 `seconds_since_success` 并判定 `stalled`。从未成功过的 worker 以 `started_at` 为基准，因此第一 tick 起就报错的 worker 超过阈值也会被标记停滞。

### 2. `runtime/weflow_state_summary.py`

`summarize_weflow_bridge_state()`：读取 `weflow_bridge_state.json`，输出每个 talker 的 `since` 游标、group/private 计数、`seen_raw_ids` 去重集合大小、SSE `Last-Event-ID`。不暴露原始 id。用于确认并发 talker 游标独立且单调推进、去重集合在多轮拉取间保留。

### 3. sidebar 集成

- `sidebar_weflow_start` 为每个 worker 附加一个 `WeflowWorkerMetrics`。
- 后台循环对每个 tick 计时并写入 metrics。
- `_weflow_worker_state` 暴露 `metrics` 快照。
- `build_sidebar_weflow_state` 增加 `bridge_state` 字段。
- `ui/sidebar/app.js` 的 `renderWeFlow` 显示停滞警告、慢 tick 计数、会话游标/去重计数，并在状态框展开 `stability` 与 `bridge_state`。

不改动任何公开 HTTP 端点签名或既有 UI 契约。

## 测试

- 新增 `tests/test_weflow_worker_metrics.py`（8 条）：总量累计、停滞判定（含从未成功场景）、慢 tick/错误计数、ring buffer 有界、状态摘要 absent/ok/unreadable。
- `tests/test_sidebar_api.py` 新增 2 条：后台循环跨 tick 复用同一 context；总失败后重建 context。
- 全量：`python -m unittest discover -s tests` → 325 OK, skipped=1。

## 安全边界

- 全部为本地文件读写与内存统计，不获取鼠标/前台窗口焦点，不走 `windows_guarded` 发送路径。
- 未新增任何远程网络能力；主抓取仍是本地 WeFlow HTTP pull。

## 下一步（仍需实机）

代码地基已就绪。真正的 P1 验收仍需启动 WeFlow fork 后连续运行后台 worker，用 sidebar 观察 `metrics.stalled`、`slow_ticks`、`bridge_state.sessions[].since` 是否随真实消息推进。
