const state = {
  data: null,
  activeStatus: "pending",
  refreshing: false,
  controlsDirty: false,
  controlsSaving: false,
  actionInProgress: false,
  manualProbe: null,
  statusMessage: "",
};

const $ = (selector) => document.querySelector(selector);

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
    $("#readinessLine").textContent = `Load failed: ${error.message}`;
  } finally {
    state.refreshing = false;
  }
}

function render({ forceControls = false } = {}) {
  const data = state.data;
  if (!data) return;
  if (forceControls || (!state.controlsDirty && !state.controlsSaving)) {
    syncControls(data.config);
  }

  const readiness = data.readiness;
  $("#readinessLine").textContent =
    state.statusMessage ||
    `${readiness.status} | blockers ${readiness.summary.blockers} | warnings ${readiness.summary.warnings}`;

  $("#pendingCount").textContent = data.queues.pending.count;
  $("#approvedCount").textContent = data.queues.approved.count;
  $("#failedCount").textContent = data.queues.failed.count;

  renderQueue();
  renderAudit();
  renderProbe();
}

function syncControls(config) {
  $("#modeSelect").value = config.mode;
  $("#sendEnabled").checked = config.send_enabled;
  $("#driverSelect").value = config.send_driver;
  state.controlsDirty = false;
  setDirtyIndicator("clean");
}

function renderQueue() {
  const list = $("#queueList");
  const queue = state.data.queues[state.activeStatus] || { items: [] };
  list.innerHTML = "";
  if (!queue.items.length) {
    list.append(emptyNode("No queue items"));
    return;
  }
  for (const item of queue.items) {
    const reply = item.reply || {};
    const node = document.createElement("article");
    node.className = "queue-item";
    node.innerHTML = `
      <div class="queue-meta">
        <span>${escapeHtml(item.status || "")}</span>
        <span>${escapeHtml(reply.conversation_id || "")}</span>
        <span>${escapeHtml(reply.model || "")}</span>
      </div>
      <div class="reply-text">${escapeHtml(reply.text || "")}</div>
      <div class="actions"></div>
    `;
    const actions = node.querySelector(".actions");
    if (item.status === "pending") {
      actions.append(actionButton("Approve", "soft", () => queueAction(item.queue_id, "approve")));
      actions.append(actionButton("Reject", "danger", () => queueAction(item.queue_id, "reject")));
    }
    if (item.status === "approved") {
      actions.append(actionButton("Send after 3s", "primary", () => delayedQueueAction(item.queue_id, "send-approved")));
      actions.append(actionButton("Reject", "danger", () => queueAction(item.queue_id, "reject")));
    }
    list.append(node);
  }
}

function renderAudit() {
  const list = $("#auditList");
  list.innerHTML = "";
  const items = state.data.audit.items || [];
  if (!items.length) {
    list.append(emptyNode("No audit records"));
    return;
  }
  for (const item of items.slice().reverse()) {
    const node = document.createElement("article");
    node.className = "audit-item";
    node.innerHTML = `
      <div class="audit-meta">
        <span>${escapeHtml(item.action || "")}</span>
        <span>${escapeHtml(item.status || "")}</span>
        <span>${escapeHtml(item.queue_id || "")}</span>
      </div>
      <div class="reply-text">${escapeHtml(item.reason || item.note || item.timestamp || "")}</div>
    `;
    list.append(node);
  }
}

function renderProbe() {
  const payload = state.manualProbe || state.data.driver_probe;
  $("#probeBox").textContent = JSON.stringify(payload, null, 2);
}

async function saveControls() {
  if (state.controlsSaving) return;
  state.controlsSaving = true;
  setDirtyIndicator("saving");
  try {
    await api("/api/controls", {
      method: "POST",
      body: JSON.stringify({
        mode: $("#modeSelect").value,
        send_enabled: $("#sendEnabled").checked,
        send_driver: $("#driverSelect").value,
      }),
    });
    state.controlsDirty = false;
    state.manualProbe = null;
    setStatusMessage("controls saved");
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
  setStatusMessage(`${action} ok`);
  state.manualProbe = null;
  await refresh({ force: true });
  return payload;
}

async function delayedQueueAction(queueId, action) {
  await countdown("Switch to the target WeChat chat", 3);
  await queueAction(queueId, action);
}

async function probeWindowsGuarded() {
  await countdown("Switch to the target WeChat chat", 3);
  const payload = await api("/api/driver-probe?driver=windows_guarded");
  state.manualProbe = payload.probe;
  renderProbe();
}

function markControlsDirty() {
  state.controlsDirty = true;
  setDirtyIndicator("dirty");
}

function setDirtyIndicator(status) {
  const button = $("#saveControls");
  button.disabled = status === "saving";
  button.textContent = status === "saving" ? "Saving..." : (status === "dirty" ? "Save controls *" : "Save controls");
  button.classList.toggle("dirty", status === "dirty");
}

function setStatusMessage(message) {
  state.statusMessage = message;
  $("#readinessLine").textContent = message;
  setTimeout(() => {
    if (state.statusMessage === message) {
      state.statusMessage = "";
    }
  }, 2500);
}

function countdown(prefix, seconds) {
  return new Promise((resolve) => {
    let remaining = seconds;
    $("#readinessLine").textContent = `${prefix}: ${remaining}s`;
    const timer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(timer);
        resolve();
        return;
      }
      $("#readinessLine").textContent = `${prefix}: ${remaining}s`;
    }, 1000);
  });
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
      $("#readinessLine").textContent = `Action failed: ${error.message}`;
    } finally {
      button.disabled = false;
      state.actionInProgress = false;
    }
  });
  return button;
}

function emptyNode(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (!tab) return;
  setActiveStatus(tab.dataset.status);
  renderQueue();
});

function setActiveStatus(status) {
  state.activeStatus = status;
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.status === status);
  });
}

["modeSelect", "sendEnabled", "driverSelect"].forEach((id) => {
  $(`#${id}`).addEventListener("change", markControlsDirty);
});

$("#refreshButton").addEventListener("click", () => refresh({ forceControls: !state.controlsDirty }));
$("#saveControls").addEventListener("click", () => saveControls().catch((error) => {
  $("#readinessLine").textContent = `Save failed: ${error.message}`;
}));
$("#probeButton").addEventListener("click", () => probeWindowsGuarded().catch((error) => {
  $("#probeBox").textContent = `Probe failed: ${error.message}`;
}));

refresh({ forceControls: true });
setInterval(() => {
  if (!state.actionInProgress && !state.controlsSaving) {
    refresh();
  }
}, 2000);
