const state = {
  data: null,
  activeStatus: "pending",
  refreshing: false,
  controlsDirty: false,
  controlsSaving: false,
  actionInProgress: false,
  statusMessage: "",
  probeExpanded: false,
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
  return names.length ? names : ["not_implemented", "windows_guarded"];
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
    note.textContent = `已隐藏 ${channels.hidden_count} 个旧探测/乱码通道：${reasonSummary(channels.hidden_reasons || {})}`;
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
    `;
    list.append(node);
  }
}

function renderCounts(data) {
  $("#pendingCount").textContent = data.queues?.pending?.count || 0;
  $("#approvedCount").textContent = data.queues?.approved?.count || 0;
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

async function probeNow() {
  const payload = await api("/api/wechat-probe");
  state.data = { ...(state.data || {}), wechat_window_probe: payload };
  renderWechatProbe(payload);
  renderProbeJson();
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
    dry_run: "演练",
    queued_for_confirm: "待审核",
    skipped: "跳过",
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
$("#toggleProbe").addEventListener("click", () => {
  state.probeExpanded = !state.probeExpanded;
  renderProbeJson();
});

refresh({ forceControls: true });
setInterval(() => {
  if (!state.actionInProgress && !state.controlsSaving) refresh();
}, 1800);
