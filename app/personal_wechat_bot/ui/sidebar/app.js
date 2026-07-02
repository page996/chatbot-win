const state = {
  data: null,
  activeStatus: "pending",
  refreshing: false,
  controlsDirty: false,
  controlsSaving: false,
  actionInProgress: false,
  statusMessage: "",
  probeExpanded: false,
  activePanel: "queue",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.status === "error") {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

async function refresh({ forceControls = false, force = false } = {}) {
  if (state.refreshing || ((state.actionInProgress || state.controlsSaving) && !force)) return;
  state.refreshing = true;
  try {
    state.data = await api("/api/state");
    render({ forceControls });
  } catch (error) {
    $("#readinessLine").textContent = `加载失败：${error.message}`;
  } finally {
    state.refreshing = false;
  }
}

function render({ forceControls = false } = {}) {
  const data = state.data;
  if (!data) return;
  if (forceControls || (!state.controlsDirty && !state.controlsSaving)) {
    syncControls(data);
  }
  renderReadiness(data);
  renderCapture(data);
  renderChannels(data);
  renderWechatProbe(data.wechat_window_probe || {});
  renderCounts(data);
  renderQueue();
  renderBridge(data.send_bridge || {});
  renderRuntimeCards(data.runtime_cards || {});
  renderWeFlow(data.weflow || {});
  renderAudit();
  renderProbeJson();
}

function renderReadiness(data) {
  const readiness = data.readiness || {};
  const summary = readiness.summary || {};
  $("#readinessLine").textContent =
    state.statusMessage ||
    `${statusText(readiness.status || "unknown")} / 阻断 ${summary.blockers || 0} / 警告 ${summary.warnings || 0}`;
}

function renderCapture(data) {
  const capture = data.capture || {};
  $("#captureRole").textContent = sourceRoleText(capture.owner || "backend_message_sources");
  $("#captureDetail").textContent = [
    roleText(capture.sidebar_role || "audit_and_send_controls_only"),
    capture.supports_multi_conversation ? "多会话隔离已开启" : "",
    capture.background_send_status ? `非前台发送：${backgroundSendText(capture.background_send_status)}` : "",
    capture.window_probe_role ? `窗口探测：${roleText(capture.window_probe_role)}` : "",
  ].filter(Boolean).join(" / ");
}

function syncControls(data) {
  const config = data.config || {};
  setMode(config.mode || "dry_run");
  $("#sendEnabled").checked = Boolean(config.send_enabled);
  const drivers = driverNames(data);
  const driverSelect = $("#driverSelect");
  driverSelect.innerHTML = "";
  for (const driver of drivers) {
    const option = document.createElement("option");
    option.value = driver;
    option.textContent = driver;
    driverSelect.append(option);
  }
  driverSelect.value = config.send_driver || drivers[0] || "not_implemented";
  state.controlsDirty = false;
  setDirtyIndicator("clean");
}

function driverNames(data) {
  const registered = data.driver_probe?.registered_send_drivers || [];
  const names = registered.map((item) => item.name).filter(Boolean);
  const configured = data.config?.send_driver;
  if (configured && !names.includes(configured)) names.unshift(configured);
  return names.length ? names : ["not_implemented", "windows_guarded", "bridge_outbox"];
}

function renderWechatProbe(probe) {
  const active = probe.active || {};
  const foreground = probe.foreground || {};
  const windows = probe.windows || [];
  const first = windows[0] || {};
  $("#diagnosticDetail").textContent = [
    active.title || foreground.title || first.title || "未发现可用微信聊天窗口",
    probeStatusText(active.status || probe.status || "unknown"),
    foreground.process_name || first.process_name || "",
    first.hwnd ? `hwnd ${first.hwnd}` : "仅诊断",
    probe.ui_automation?.available ? "UIA 可用" : (probe.ui_automation?.reason || "UIA 未知"),
  ].filter(Boolean).join(" / ");

  const list = $("#handleList");
  list.innerHTML = "";
  if (!windows.length) {
    list.append(emptyNode("没有发现可用的微信窗口句柄"));
    return;
  }
  for (const windowInfo of windows.slice(0, 2)) {
    const node = document.createElement("article");
    node.className = "handle-item";
    const candidates = windowInfo.chat_candidates || [];
    node.innerHTML = `
      <div class="handle-main">
        <strong>${escapeHtml(windowInfo.title || "(无标题)")}</strong>
        <span>${escapeHtml(windowInfo.process_name || "")} / hwnd ${escapeHtml(windowInfo.hwnd || "")}</span>
      </div>
      <div class="handle-sub">
        子窗口 ${windowInfo.child_count || 0} / 控件 ${windowInfo.automation_control_count || 0}
      </div>
    `;
    if (candidates.length) {
      const candidateList = document.createElement("div");
      candidateList.className = "candidate-list";
      for (const candidate of candidates.slice(0, 4)) {
        const pill = document.createElement("span");
        pill.textContent = `${candidate.control_type || "control"} ${candidate.name || candidate.class_name || ""}`.trim();
        candidateList.append(pill);
      }
      node.append(candidateList);
    }
    list.append(node);
  }
}

function renderChannels(data) {
  const channels = data.channels || { items: [] };
  const items = channels.items || [];
  $("#channelCount").textContent = channels.count || items.length || 0;
  $("#privateCount").textContent = channels.private_count || 0;
  $("#groupCount").textContent = channels.group_count || 0;
  $("#hiddenChannelCount").textContent = channels.hidden_count || 0;
  const list = $("#channelList");
  list.innerHTML = "";
  if (channels.hidden_count) {
    const note = document.createElement("div");
    note.className = "channel-note";
    note.innerHTML = `
      <span>已隐藏 ${channels.hidden_count} 个旧探测/乱码通道：${escapeHtml(reasonSummary(channels.hidden_reasons || {}))}</span>
      <button class="ghost small" type="button">清理隐藏通道</button>
    `;
    note.querySelector("button").addEventListener("click", () => cleanupHiddenChannels());
    list.append(note);
  }
  if (!items.length) {
    list.append(emptyNode("还没有可信后端服务通道"));
    return;
  }
  for (const channel of items.slice(0, 8)) {
    const node = document.createElement("article");
    node.className = "channel-item";
    node.innerHTML = `
      <div class="channel-main">
        <strong>${escapeHtml(channel.chat_title || channel.conversation_id || "(无标题)")}</strong>
        <span>${escapeHtml(conversationTypeText(channel.conversation_type || ""))}</span>
      </div>
      <p>${escapeHtml(channel.conversation_id || "")}</p>
      <div class="channel-meta">
        <span>key 槽 ${(channel.api_key_refs || []).length || channel.key_slots || 0}</span>
        <span>${escapeHtml(channel.session_scope || "独立 session")}</span>
        <span>${escapeHtml(shortTime(channel.updated_at || ""))}</span>
      </div>
      <div class="channel-actions"></div>
    `;
    const actions = node.querySelector(".channel-actions");
    actions.append(actionButton("清除通道", "ghost small", () => deleteChannel(channel.conversation_id)));
    list.append(node);
  }
}

function renderCounts(data) {
  $("#pendingCount").textContent = data.queues?.pending?.count || 0;
  $("#approvedCount").textContent = data.queues?.approved?.count || 0;
  $("#bridgeQueuedCount").textContent = data.queues?.queued_to_bridge?.count || 0;
  $("#rejectedCount").textContent = data.queues?.rejected?.count || 0;
  $("#sentCount").textContent = data.queues?.sent?.count || 0;
  $("#failedCount").textContent = data.queues?.failed?.count || 0;
}

function renderQueue() {
  const list = $("#queueList");
  const queue = state.data?.queues?.[state.activeStatus] || { items: [] };
  list.innerHTML = "";
  if (!queue.items.length) {
    list.append(emptyNode(`${queueStatusText(state.activeStatus)}队列为空`));
    return;
  }
  for (const item of queue.items) {
    const reply = item.reply || {};
    const node = document.createElement("article");
    node.className = `queue-item status-${item.status || state.activeStatus}`;
    node.innerHTML = `
      <div class="queue-head">
        <span>${escapeHtml(queueStatusText(item.status || state.activeStatus))}</span>
        <time>${escapeHtml(shortTime(item.updated_at || reply.created_at || ""))}</time>
      </div>
      <div class="conversation">${escapeHtml(reply.conversation_id || "")}</div>
      <div class="reply-text">${escapeHtml(reply.text || "")}</div>
      <div class="actions"></div>
    `;
    const actions = node.querySelector(".actions");
    if (item.status === "pending") {
      actions.append(actionButton("通过", "primary", () => queueAction(item.queue_id, "approve")));
      actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject")));
    }
    if (item.status === "approved") {
      actions.append(actionButton("3秒后发送", "primary", () => delayedQueueAction(item.queue_id, "send-approved")));
      actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject")));
    }
    list.append(node);
  }
}

function renderBridge(bridge) {
  const manualCount = bridge.manual_bound_count || 0;
  $("#bridgePendingCount").textContent = `${bridge.pending_count || 0} 待消费 / ${manualCount} 手动通道`;
  $("#bridgePath").textContent = manualCount
    ? (bridge.outbox_path || "未创建 outbox")
    : "仅手动抓取并绑定过的微信通道可进入非前台桥";
  const list = $("#bridgeList");
  list.innerHTML = "";
  const items = bridge.items || [];
  if (!items.length) {
    list.append(emptyNode("非前台桥 outbox 为空"));
    return;
  }
  for (const item of items.slice(-10).reverse()) {
    const node = document.createElement("article");
    node.className = `bridge-item status-${item.status || "queued"}`;
    node.innerHTML = `
      <div class="queue-head">
        <span>${escapeHtml(bridgeStatusText(item.status || "queued"))}</span>
        <time>${escapeHtml(shortTime(item.created_at || ""))}</time>
      </div>
      <div class="conversation">${escapeHtml(item.conversation_id || "")}</div>
      <div class="reply-text">${escapeHtml(item.text || "")}</div>
      <p>${escapeHtml(item.bridge_id || "")}</p>
      <div class="actions"></div>
    `;
    const actions = node.querySelector(".actions");
    if ((item.status || "queued") === "queued") {
      actions.append(actionButton("标记已发", "primary", () => ackBridge(item.bridge_id, "sent")));
      actions.append(actionButton("标记失败", "danger", () => ackBridge(item.bridge_id, "failed")));
    }
    list.append(node);
  }
}

function renderRuntimeCards(runtimeCards) {
  const active = runtimeCards.active || {};
  const catalog = runtimeCards.catalog || [];
  const activeSkillIds = new Set((active.skills || []).map((item) => item.card_id));
  const activeTaskIds = new Set((active.tasks || []).map((item) => item.card_id));
  const persona = active.persona || {};
  $("#runtimePolicy").textContent = policyText(runtimeCards.policy || "");
  $("#runtimeStorage").textContent = runtimeCards.storage || "data/runtime_cards";
  $("#skillCount").textContent = `${activeSkillIds.size} 启用`;
  $("#personaName").textContent = persona.name || "未装备";
  $("#taskCount").textContent = `${activeTaskIds.size} 装备`;
  renderCardList({
    root: $("#skillList"),
    cards: catalog.filter((item) => item.card_type === "skill"),
    activeIds: activeSkillIds,
    emptyText: "暂无技能卡",
    actionFor: (card, activeNow) =>
      actionButton(activeNow ? "停用" : "启用", activeNow ? "ghost small" : "primary small", () =>
        runtimeCardAction(activeNow ? "disable-skill" : "enable-skill", { card_id: card.card_id }),
      ),
  });
  renderCardList({
    root: $("#personaList"),
    cards: catalog.filter((item) => item.card_type === "persona"),
    activeIds: new Set(persona.card_id ? [persona.card_id] : []),
    emptyText: "暂无人物卡",
    actionFor: (card, activeNow) =>
      actionButton(activeNow ? "已装备" : "装备", activeNow ? "ghost small" : "primary small", () =>
        activeNow ? Promise.resolve() : runtimeCardAction("equip-persona", { card_id: card.card_id }),
      ),
  });
  renderCardList({
    root: $("#taskList"),
    cards: catalog.filter((item) => item.card_type === "task"),
    activeIds: activeTaskIds,
    emptyText: "暂无任务卡",
    actionFor: (card, activeNow) =>
      actionButton(activeNow ? "卸载" : "装备", activeNow ? "danger small" : "primary small", () =>
        runtimeCardAction(activeNow ? "unload-task" : "equip-task", { card_id: card.card_id }),
      ),
  });
}

function renderWeFlow(weflow) {
  const worker = weflow.worker || {};
  const metrics = worker.metrics || {};
  const bridgeState = weflow.bridge_state || {};
  $("#weflowStatus").textContent = worker.running
    ? `后台运行中 / ${worker.loops || 0} 轮`
    : (weflow.last_pull?.status || weflow.last_health?.status || "未运行");
  $("#weflowDetail").textContent = [
    weflow.security?.primary_source || "weflow_local_fork",
    weflow.security?.requires_token_for_pull ? "正式拉取需要 token" : "",
    weflow.security?.requires_local_fork_marker ? "需要本地 fork marker" : "",
    weflow.config_migration?.status === "updated" ? "扩展配置已迁移" : "",
    metrics.stalled ? "⚠ 后台疑似停滞（长时间无成功拉取）" : "",
    metrics.slow_ticks ? `慢 tick ${metrics.slow_ticks} 次` : "",
    bridgeState.session_count ? `会话游标 ${bridgeState.session_count} / 去重 ${bridgeState.seen_raw_id_count || 0}` : "",
    worker.last_error ? `后台错误：${worker.last_error}` : "",
  ].filter(Boolean).join(" / ");
  if (!$("#weflowBaseUrl").dataset.touched) $("#weflowBaseUrl").value = weflow.base_url || "http://127.0.0.1:5031";
  if (!$("#weflowTokenEnv").dataset.touched) $("#weflowTokenEnv").value = weflow.token_env || "WEFLOW_API_TOKEN";
  const statusPayload = {
    worker,
    stability: {
      running: worker.running || false,
      stalled: metrics.stalled || false,
      loops: metrics.loops || 0,
      error_ticks: metrics.error_ticks || 0,
      slow_ticks: metrics.slow_ticks || 0,
      totals: metrics.totals || {},
      seconds_since_success: metrics.seconds_since_success ?? null,
      seconds_since_progress: metrics.seconds_since_progress ?? null,
      max_tick_duration_seconds: metrics.max_tick_duration_seconds || 0,
      recent_ticks: metrics.recent_ticks || [],
    },
    bridge_state: bridgeState,
    last_health: weflow.last_health || {},
    last_pull: compactPayload(weflow.last_pull || {}, 1800),
    last_backfill: compactPayload(weflow.last_backfill || {}, 1800),
    files: {
      hook_event_file: weflow.hook_event_file,
      backend_event_file: weflow.backend_event_file,
      weflow_state_file: weflow.weflow_state_file,
    },
    config_migration: weflow.config_migration || {},
  };
  $("#weflowStatusBox").textContent = JSON.stringify(statusPayload, null, 2);
}

function renderCardList({ root, cards, activeIds, emptyText, actionFor }) {
  root.innerHTML = "";
  if (!cards.length) {
    root.append(emptyNode(emptyText));
    return;
  }
  for (const card of cards) {
    const activeNow = activeIds.has(card.card_id);
    const node = document.createElement("article");
    node.className = `runtime-card ${activeNow ? "active" : ""}`;
    node.innerHTML = `
      <div class="runtime-card-head">
        <div>
          <strong>${escapeHtml(card.name || card.card_id)}</strong>
          <span>${escapeHtml(cardTypeText(card.card_type))} / ${escapeHtml(card.source || "custom")}</span>
        </div>
        <span class="card-state">${activeNow ? "生效中" : "未生效"}</span>
      </div>
      <p>${escapeHtml(compactText(card.content || "", 220))}</p>
      <div class="actions"></div>
    `;
    node.querySelector(".actions").append(actionFor(card, activeNow));
    root.append(node);
  }
}

function renderAudit() {
  const list = $("#auditList");
  const items = state.data?.audit?.items || [];
  $("#auditCount").textContent = items.length;
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyNode("暂无发送审计记录"));
    return;
  }
  for (const item of items.slice(-8).reverse()) {
    const node = document.createElement("article");
    node.className = "audit-item";
    node.innerHTML = `
      <span>${escapeHtml(actionText(item.action || ""))}</span>
      <strong>${escapeHtml(queueStatusText(item.status || ""))}</strong>
      <p>${escapeHtml(item.reason || item.note || item.queue_id || "")}</p>
    `;
    list.append(node);
  }
}

function renderProbeJson() {
  $("#probeBox").hidden = !state.probeExpanded;
  if (!state.probeExpanded) return;
  $("#probeBox").textContent = JSON.stringify(
    {
      driver_probe: state.data?.driver_probe,
      wechat_window_probe: state.data?.wechat_window_probe,
    },
    null,
    2,
  );
}

async function saveControls() {
  if (state.controlsSaving) return;
  state.controlsSaving = true;
  setDirtyIndicator("saving");
  try {
    await api("/api/controls", {
      method: "POST",
      body: JSON.stringify({
        mode: currentMode(),
        send_enabled: $("#sendEnabled").checked,
        send_driver: $("#driverSelect").value,
      }),
    });
    state.controlsDirty = false;
    setStatusMessage("发送控制已保存");
    await refresh({ forceControls: true, force: true });
  } finally {
    state.controlsSaving = false;
    setDirtyIndicator(state.controlsDirty ? "dirty" : "clean");
  }
}

async function queueAction(queueId, action) {
  const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({ reviewer: "sidebar" }),
  });
  const nextStatus = payload.item?.status || payload.status;
  if (nextStatus && state.data?.queues?.[nextStatus]) {
    setActiveStatus(nextStatus);
  }
  setStatusMessage(`${actionText(action)}完成`);
  await refresh({ force: true });
  return payload;
}

async function delayedQueueAction(queueId, action) {
  await countdown("请切到目标微信聊天窗口", 3);
  await queueAction(queueId, action);
}

async function deleteChannel(conversationId) {
  if (!conversationId) return;
  const payload = await api(`/api/channels/delete/${encodeURIComponent(conversationId)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatusMessage(payload.note || "通道已清除");
  await refresh({ force: true });
}

async function cleanupHiddenChannels() {
  const payload = await api("/api/channels/cleanup-hidden", {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatusMessage(payload.note || "隐藏通道已清理");
  await refresh({ force: true });
}

async function ackBridge(bridgeId, status) {
  if (!bridgeId) return;
  await api("/api/bridge/ack", {
    method: "POST",
    body: JSON.stringify({
      bridge_id: bridgeId,
      status,
      reason: status === "sent" ? "manual_sidebar_ack" : "manual_sidebar_failed",
    }),
  });
  setStatusMessage(status === "sent" ? "桥接项已标记为已发" : "桥接项已标记为失败");
  await refresh({ force: true });
}

async function runtimeCardAction(action, payload) {
  await api(`/api/runtime-cards/${action}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setStatusMessage("技能/人设卡已更新");
  await refresh({ force: true });
}

async function savePersonaCard(event) {
  event.preventDefault();
  const name = $("#personaCardName").value.trim();
  const content = $("#personaCardContent").value.trim();
  if (!content) {
    setStatusMessage("人物卡内容不能为空");
    return;
  }
  await runtimeCardAction("save-persona", { name, content });
  $("#personaCardName").value = "";
  $("#personaCardContent").value = "";
}

async function saveTaskCard(event) {
  event.preventDefault();
  const name = $("#taskCardName").value.trim();
  const content = $("#taskCardContent").value.trim();
  if (!content) {
    setStatusMessage("任务卡内容不能为空");
    return;
  }
  await runtimeCardAction("save-task", { name, content });
  $("#taskCardName").value = "";
  $("#taskCardContent").value = "";
}

async function probeNow() {
  const payload = await api("/api/wechat-probe");
  state.data = { ...(state.data || {}), wechat_window_probe: payload };
  renderWechatProbe(payload);
  renderProbeJson();
}

function weflowPayload(extra = {}) {
  return {
    base_url: $("#weflowBaseUrl").value.trim() || "http://127.0.0.1:5031",
    token_env: $("#weflowTokenEnv").value.trim() || "WEFLOW_API_TOKEN",
    token: $("#weflowToken").value.trim(),
    talkers: splitComma($("#weflowTalkers").value),
    workers: Number($("#weflowWorkers").value || 2),
    message_limit: Number($("#weflowMessageLimit").value || 100),
    max_pages: Number($("#weflowMaxPages").value || 1),
    lookback_seconds: Number($("#weflowLookback").value || 300),
    interval_seconds: Number($("#weflowInterval").value || 5),
    context_only: $("#weflowContextOnly").checked,
    ...extra,
  };
}

async function weflowAction(action, extra = {}) {
  const payload = await api(`/api/weflow/${action}`, {
    method: "POST",
    body: JSON.stringify(weflowPayload(extra)),
  });
  $("#weflowStatusBox").textContent = JSON.stringify(compactPayload(payload, 5000), null, 2);
  setStatusMessage(`WeFlow ${action} 完成`);
  await refresh({ force: true });
  return payload;
}

async function weflowBackfill() {
  const talkers = splitComma($("#weflowTalkers").value);
  if (!talkers.length) {
    $("#weflowStatusBox").textContent = "回填历史需要在 Talkers 填写要初始化的会话 id（不能为空）。";
    setStatusMessage("回填历史需要指定 Talkers");
    return;
  }
  if (!window.confirm(`将从头拉取以下会话的历史消息作为上下文（不会回复旧消息）：\n${talkers.join(", ")}`)) return;
  const payload = await api("/api/weflow/backfill", {
    method: "POST",
    body: JSON.stringify(weflowPayload({ talkers })),
  });
  $("#weflowStatusBox").textContent = JSON.stringify(compactPayload(payload, 5000), null, 2);
  setStatusMessage(`WeFlow 回填历史完成（${(payload.backfilled_talkers || []).length} 个会话）`);
  await refresh({ force: true });
  return payload;
}

async function weflowDependencies() {
  const payload = await api("/api/weflow/dependencies", {
    method: "POST",
    body: JSON.stringify({}),
  });
  $("#weflowStatusBox").textContent = JSON.stringify(payload, null, 2);
  setStatusMessage("WeFlow 依赖检查完成");
}

async function weflowInstallDeps() {
  if (!window.confirm("将使用当前 Python 执行 pip install -r requirements-ocr.txt")) return;
  const payload = await api("/api/weflow/install-deps", {
    method: "POST",
    body: JSON.stringify({ confirm_install: true }),
  });
  $("#weflowStatusBox").textContent = JSON.stringify(compactPayload(payload, 6000), null, 2);
  setStatusMessage("WeFlow 依赖安装完成");
}

function splitComma(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function actionButton(label, className, handler) {
  const button = document.createElement("button");
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async () => {
    if (state.actionInProgress) return;
    state.actionInProgress = true;
    button.disabled = true;
    try {
      await handler();
    } catch (error) {
      $("#readinessLine").textContent = `操作失败：${error.message}`;
    } finally {
      button.disabled = false;
      state.actionInProgress = false;
    }
  });
  return button;
}

function setActiveStatus(status) {
  state.activeStatus = status;
  $$(".metric").forEach((item) => item.classList.toggle("active", item.dataset.status === status));
  renderQueue();
}

function setActivePanel(panel) {
  state.activePanel = panel;
  $$(".bookmark-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panel);
  });
  $("#queuePanel").hidden = panel !== "queue";
  $("#bridgePanel").hidden = panel !== "bridge";
  $("#runtimePanel").hidden = panel !== "runtime";
  $("#weflowPanel").hidden = panel !== "weflow";
}

function setMode(mode) {
  $$(".segmented button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
}

function currentMode() {
  return $(".segmented button.active")?.dataset.mode || "dry_run";
}

function markControlsDirty() {
  state.controlsDirty = true;
  setDirtyIndicator("dirty");
}

function setDirtyIndicator(status) {
  const button = $("#saveControls");
  button.disabled = status === "saving";
  button.textContent = status === "saving" ? "保存中" : (status === "dirty" ? "保存 *" : "保存");
  button.classList.toggle("dirty", status === "dirty");
}

function setStatusMessage(message) {
  state.statusMessage = message;
  $("#readinessLine").textContent = message;
  setTimeout(() => {
    if (state.statusMessage === message) state.statusMessage = "";
  }, 2500);
}

function countdown(prefix, seconds) {
  return new Promise((resolve) => {
    let remaining = seconds;
    $("#readinessLine").textContent = `${prefix}：${remaining}s`;
    const timer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(timer);
        resolve();
        return;
      }
      $("#readinessLine").textContent = `${prefix}：${remaining}s`;
    }, 1000);
  });
}

function emptyNode(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function shortTime(value) {
  if (!value) return "";
  const afterT = String(value).includes("T") ? String(value).split("T")[1] : String(value);
  return afterT.split(/[+.]/)[0].slice(0, 8);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function statusText(status) {
  return {
    ok: "正常",
    warn: "警告",
    error: "错误",
    unknown: "未知",
  }[status] || status;
}

function queueStatusText(status) {
  return {
    pending: "待审核",
    approved: "已通过",
    rejected: "已拒绝",
    sent: "已发送",
    failed: "失败",
    queued: "已入桥",
    queued_to_bridge: "已入非前台桥",
    dry_run: "演练",
    queued_for_confirm: "待审核",
    skipped: "跳过",
  }[status] || status || "";
}

function bridgeStatusText(status) {
  return {
    queued: "待桥接",
    sent: "已确认发送",
    failed: "发送失败",
    blocked: "已阻断",
  }[status] || status || "";
}

function conversationTypeText(value) {
  return value === "group" ? "群聊" : (value === "private" ? "私聊" : value);
}

function sourceRoleText(value) {
  return {
    backend_message_sources: "后端消息源负责读取对话",
  }[value] || value;
}

function roleText(value) {
  return {
    audit_and_send_controls_only: "浮窗只做审计和发送控制",
    diagnostic_only: "仅诊断",
  }[value] || value;
}

function backgroundSendText(value) {
  return {
    not_supported_by_windows_guarded: "windows_guarded 需要前台",
    bridge_outbox_available: "bridge_outbox 可入队",
    bridge_outbox_configured_disabled: "bridge_outbox 已配置，发送未启用",
    bridge_outbox_ready: "bridge_outbox 已启用",
    bridge_outbox_manual_capture_only_available: "仅手动抓取通道可用",
    bridge_outbox_waiting_for_manual_capture: "等待手动抓取通道",
    bridge_outbox_ready_for_manual_channels: "已启用，仅限手动通道",
  }[value] || value;
}

function policyText(value) {
  return {
    runtime_cards_survive_context_reset_sidebar_only_changes: "清空上下文不会影响卡片，只有此页会改变装备状态",
  }[value] || value || "清空上下文不会影响已装备卡片";
}

function cardTypeText(value) {
  return {
    skill: "技能",
    persona: "人物",
    task: "任务",
  }[value] || value || "";
}

function compactText(value, maxLength) {
  const text = String(value).replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1).trim()}…`;
}

function compactPayload(value, maxLength) {
  const text = JSON.stringify(value || {}, null, 2);
  if (text.length <= maxLength) return value;
  return {
    compacted: true,
    preview: text.slice(0, maxLength - 1).trim() + "…",
  };
}

function probeStatusText(value) {
  return {
    ok: "已找到微信窗口",
    not_found: "未找到微信窗口",
    matched_foreground: "匹配当前前台",
    foreground_wechat_child_or_popup: "前台是微信子窗口",
    not_wechat_foreground: "前台不是微信",
    unknown: "未知",
  }[value] || value;
}

function actionText(action) {
  return {
    approve: "通过",
    reject: "拒绝",
    "send-approved": "发送",
  }[action] || action;
}

function reasonSummary(reasons) {
  const labels = {
    probe_fragment: "探测碎片",
    untrusted_legacy_channel: "旧污染通道",
    mojibake: "乱码标题",
    tool_window: "工具窗口",
    empty_title: "空标题",
  };
  return Object.entries(reasons)
    .map(([key, count]) => `${labels[key] || key} ${count}`)
    .join("，");
}

document.addEventListener("click", (event) => {
  const metric = event.target.closest(".metric");
  if (metric) {
    setActiveStatus(metric.dataset.status);
    return;
  }
  const modeButton = event.target.closest(".segmented button");
  if (modeButton) {
    setMode(modeButton.dataset.mode);
    markControlsDirty();
    return;
  }
  const panelButton = event.target.closest(".bookmark-tabs button");
  if (panelButton) {
    setActivePanel(panelButton.dataset.panel);
  }
});

$("#sendEnabled").addEventListener("change", markControlsDirty);
$("#driverSelect").addEventListener("change", markControlsDirty);
$("#refreshButton").addEventListener("click", () => refresh({ forceControls: !state.controlsDirty, force: true }));
$("#saveControls").addEventListener("click", () => saveControls().catch((error) => {
  $("#readinessLine").textContent = `保存失败：${error.message}`;
}));
$("#probeButton").addEventListener("click", () => probeNow().catch((error) => {
  $("#diagnosticDetail").textContent = `探测失败：${error.message}`;
}));
$("#bridgeRefreshButton").addEventListener("click", () => refresh({ force: true }));
$("#runtimeRefreshButton").addEventListener("click", () => refresh({ force: true }));
$("#weflowRefreshButton").addEventListener("click", () => refresh({ force: true }));
$("#weflowHealthButton").addEventListener("click", () => weflowAction("health").catch((error) => {
  $("#weflowStatusBox").textContent = `WeFlow health 失败：${error.message}`;
}));
$("#weflowPullButton").addEventListener("click", () => weflowAction("pull-once").catch((error) => {
  $("#weflowStatusBox").textContent = `WeFlow 拉取失败：${error.message}`;
}));
$("#weflowBackfillButton").addEventListener("click", () => weflowBackfill().catch((error) => {
  $("#weflowStatusBox").textContent = `WeFlow 回填历史失败：${error.message}`;
}));
$("#weflowStartButton").addEventListener("click", () => weflowAction("start").catch((error) => {
  $("#weflowStatusBox").textContent = `WeFlow 启动失败：${error.message}`;
}));
$("#weflowStopButton").addEventListener("click", () => weflowAction("stop").catch((error) => {
  $("#weflowStatusBox").textContent = `WeFlow 停止失败：${error.message}`;
}));
$("#weflowDepsButton").addEventListener("click", () => weflowDependencies().catch((error) => {
  $("#weflowStatusBox").textContent = `依赖检查失败：${error.message}`;
}));
$("#weflowInstallButton").addEventListener("click", () => weflowInstallDeps().catch((error) => {
  $("#weflowStatusBox").textContent = `依赖安装失败：${error.message}`;
}));
["#weflowBaseUrl", "#weflowTokenEnv"].forEach((selector) => {
  $(selector).addEventListener("input", (event) => {
    event.target.dataset.touched = "1";
  });
});
$("#personaForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.actionInProgress) return;
  state.actionInProgress = true;
  savePersonaCard(event)
    .catch((error) => {
      $("#readinessLine").textContent = `人物卡保存失败：${error.message}`;
    })
    .finally(() => {
      state.actionInProgress = false;
    });
});
$("#taskForm").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.actionInProgress) return;
  state.actionInProgress = true;
  saveTaskCard(event)
    .catch((error) => {
      $("#readinessLine").textContent = `任务卡保存失败：${error.message}`;
    })
    .finally(() => {
      state.actionInProgress = false;
    });
});
$("#toggleProbe").addEventListener("click", () => {
  state.probeExpanded = !state.probeExpanded;
  renderProbeJson();
});

refresh({ forceControls: true });
setActivePanel("queue");
setInterval(() => {
  if (!state.actionInProgress && !state.controlsSaving) refresh();
}, 1800);
