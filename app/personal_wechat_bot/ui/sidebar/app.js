const state = {
  data: null,
  activeStatus: "pending",
  refreshing: false,
  refreshPromise: null,
  pendingRefresh: null,
  dataEpoch: 0,
  controlsDirty: false,
  controlsSaving: false,
  controlsRevision: 0,
  historyResetPending: false,
  historyResetNoticeKey: "",
  statusMessage: "",
  probeExpanded: false,
  wechatProbeOverlay: null,
  wechatProbeRequestEpoch: 0,
  driverProbeOverlay: null,
  driverProbeRequestEpoch: 0,
  activePage: "overview",
  activePanel: "queue",
  weflowStatusMode: "live",
  weflowLatestStatusText: "",
  taskSeq: 0,
  tasks: [],
  taskHistory: [],
  taskScopeChains: new Map(),
  taskSyncChains: new Map(),
  backfillTaskByJobId: new Map(),
  taskPopovers: new Map(),
  taskEvents: new Map(),
  taskEventsLoading: new Set(),
  channelLaneOpenState: new Map(),
  channelLaneDrafts: new Map(),
  channelLaneComposing: new Set(),
  channelLaneMissingRefreshes: new Map(),
  channelLaneDraftPruneRevision: 0,
  successfulRefreshRevision: 0,
  personaCardRevision: 0,
  taskCardRevision: 0,
  renderingChannelLanes: false,
};

const BACKFILL_POLL_INTERVAL_MS = 1000;
const BACKFILL_STALE_TIMEOUT_MS = 15 * 60 * 1000;
const BACKFILL_MAX_WAIT_MS = 60 * 60 * 1000;
const DRIVER_PROBE_TTL_MS = 60 * 1000;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const { timeoutMs = 120000, headers = {}, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || 120000));
  try {
    const response = await fetch(path, {
      headers: { "content-type": "application/json", ...headers },
      ...fetchOptions,
      signal: controller.signal,
    });
    const raw = await response.text();
    let payload = {};
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch (error) {
        const invalid = new Error(`服务返回了无效 JSON：HTTP ${response.status}`);
        invalid.httpStatus = response.status;
        throw invalid;
      }
    }
    if (payload?.history_reset_in_progress === true) {
      state.historyResetPending = true;
      setStatusMessage("历史清理正在进行或状态无法核实；已停止所有写操作，请勿重试");
      syncTaskButtonLocks();
    }
    if (!response.ok || payload.status === "error") {
      const error = new Error(payload.error || payload.message || `HTTP ${response.status}`);
      error.payload = payload;
      error.httpStatus = response.status;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error(`请求超时：${path}`);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function refresh({ forceControls = false, force = false } = {}) {
  if (state.historyResetPending && !force) {
    return { status: "cancelled", message: "历史清理已开始" };
  }
  if (state.controlsSaving && !force) {
    return state.refreshPromise || { status: "skipped", message: "发送控制保存中" };
  }
  if (state.refreshPromise) {
    if (force || forceControls) queueRefresh({ forceControls, force });
    return state.refreshPromise;
  }
  queueRefresh({ forceControls, force });
  state.refreshPromise = drainRefreshQueue();
  return state.refreshPromise;
}

function queueRefresh({ forceControls = false, force = false } = {}) {
  const pending = state.pendingRefresh || { forceControls: false, force: false, forceControlsRevision: null };
  state.pendingRefresh = {
    forceControls: pending.forceControls || forceControls,
    force: pending.force || force,
    forceControlsRevision: forceControls ? state.controlsRevision : pending.forceControlsRevision,
  };
}

async function drainRefreshQueue() {
  state.refreshing = true;
  let result = { status: "ok" };
  try {
    while (state.pendingRefresh) {
      const request = state.pendingRefresh;
      state.pendingRefresh = null;
      const epoch = state.dataEpoch;
      try {
        const payload = await api("/api/state");
        if (epoch === state.dataEpoch && !state.historyResetPending) {
          state.data = payload;
          reconcileHistoryResetStatus(payload.history_reset);
          state.successfulRefreshRevision += 1;
          render({
            forceControls: request.forceControls && request.forceControlsRevision === state.controlsRevision,
          });
        }
        result = payload;
      } catch (error) {
        $("#readinessLine").textContent = `加载失败：${error.message}`;
        result = { status: "error", message: error.message };
      }
    }
    return result;
  } finally {
    state.refreshing = false;
    state.refreshPromise = null;
  }
}

function reconcileHistoryResetStatus(reset) {
  if (!reset || typeof reset !== "object" || reset.status === "idle") return;
  const status = String(reset.status || "unknown");
  const phase = String(reset.phase || "");
  const active = reset.active === true || reset.outcome_unknown === true;
  if (active) {
    state.historyResetPending = true;
    const message = reset.outcome_unknown === true
      ? "历史清理状态无法核实；请勿重复清空或继续写入，请检查运行状态后再处理"
      : `历史清理仍在进行${phase ? `（${phase}）` : ""}；请等待完成后重新打开 sidebar`;
    setStatusMessage(message);
    syncTaskButtonLocks();
    return;
  }
  if (reset.terminal !== true) return;
  state.historyResetPending = false;
  syncTaskButtonLocks();
  const clearResult = reset.clear_result || {};
  const noticeKey = [status, phase, reset.updated_at || "", clearResult.history_reset_id || ""].join("|");
  if (noticeKey === state.historyResetNoticeKey) return;
  state.historyResetNoticeKey = noticeKey;
  let message;
  if (status === "ok") {
    message = `上次历史清理已完成：删除 ${Number(clearResult.removed_count || 0)} 项`;
  } else if (status === "partial_error") {
    message = `上次历史清理未完整完成：${Number(clearResult.error_count || 0)} 项失败`;
  } else if (phase === "interrupted") {
    message = "上次历史清理在写入终态前中断；请检查残留状态后再重试";
  } else if (status === "blocked") {
    message = "上次历史清理被运行中的写入任务阻断";
  } else {
    message = `上次历史清理失败${phase ? `（${phase}）` : ""}`;
  }
  setStatusMessage(message);
}

function render({ forceControls = false } = {}) {
  const data = state.data;
  if (!data) return;
  if (forceControls || (!state.controlsDirty && !state.controlsSaving)) {
    syncControls(data);
  }
  renderSendControlSummary(data);
  renderReadiness(data);
  renderCapture(data);
  renderChannels(data);
  renderWechatProbe(effectiveWechatProbe(data));
  renderCounts(data);
  renderQueue();
  renderBridge(data.send_bridge || {});
  renderRuntimeCards(data.runtime_cards || {});
  renderWeFlow(data.weflow || {});
  renderAudit();
  renderTaskQueue();
  renderProbeJson();
  syncTaskButtonLocks();
  syncDriverProbeAvailability();
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
  const configuredMode = config.mode === undefined || config.mode === null || String(config.mode).trim() === "" ? "dry_run" : config.mode;
  setMode(configuredMode);
  const sendEnabled = $("#sendEnabled");
  if (sendEnabled) sendEnabled.checked = configBoolean(config.send_enabled, false);
  const drivers = driverNames(data);
  const driverSelect = $("#driverSelect");
  if (driverSelect) {
    driverSelect.innerHTML = "";
    for (const driver of drivers) {
      const option = document.createElement("option");
      option.value = driver;
      option.textContent = driver;
      driverSelect.append(option);
    }
    driverSelect.value = selectedSendDriver(config.send_driver, drivers);
  }
  const backendSelect = $("#sendBackendSelect");
  if (backendSelect) backendSelect.value = selectedSendBackend(config.send_backend);
  const ocrModeSelect = $("#ocrModeSelect");
  if (ocrModeSelect) ocrModeSelect.value = runtimeMode(config.ocr_mode);
  const asrModeSelect = $("#asrModeSelect");
  if (asrModeSelect) asrModeSelect.value = runtimeMode(config.asr_mode);
  const fileMaxInput = $("#fileMaxMb");
  if (fileMaxInput) fileMaxInput.value = String(bytesToMegabytes(config.file_max_bytes || 20 * 1024 * 1024));
  state.controlsDirty = false;
  setDirtyIndicator("clean");
  renderSendControlSummary(data);
}

function runtimeMode(value) {
  const mode = String(value || "auto").trim().toLowerCase();
  return ["auto", "gpu", "cpu"].includes(mode) ? mode : "auto";
}

function normalizeSendMode(value, fallback = "dry_run") {
  const mode = String(value ?? "").trim().toLowerCase();
  return ["dry_run", "confirm", "auto"].includes(mode) ? mode : fallback;
}

function configBoolean(value, fallback = false) {
  if (value === undefined || value === null) return fallback;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const text = String(value).trim().toLowerCase();
  if (["true", "1", "yes", "y", "on"].includes(text)) return true;
  if (["false", "0", "no", "n", "off", ""].includes(text)) return false;
  return fallback;
}

function selectedSendDriver(configured, drivers) {
  const driver = String(configured || "").trim();
  if (driver) return driver;
  if (drivers.includes("bridge_outbox")) return "bridge_outbox";
  return drivers[0] || "bridge_outbox";
}

function selectedSendBackend(configured) {
  const backend = String(configured || "wechat_native_http").trim().toLowerCase();
  return ["dry_run", "weflow_http", "wechat_native_http"].includes(backend) ? backend : "wechat_native_http";
}

function driverProbeConfigFingerprint(data = state.data) {
  const config = data?.config;
  if (!config || typeof config !== "object") return "";
  return JSON.stringify([
    String(config.send_driver || ""),
    configBoolean(config.send_enabled, false),
    selectedSendBackend(config.send_backend),
    String(config.weflow_base_url || ""),
    String(config.weflow_token_env || ""),
    String(config.weflow_send_text_path || ""),
    String(config.weflow_send_file_path || ""),
    Number(config.weflow_send_timeout_seconds ?? 35),
    String(config.wechat_native_base_url || ""),
    String(config.wechat_native_send_text_path || ""),
    String(config.wechat_native_send_image_path || ""),
    String(config.wechat_native_send_file_path || ""),
    String(config.wechat_native_status_path || ""),
    Number(config.wechat_native_timeout_seconds ?? 15),
    Number(config.wechat_native_verify_timeout_seconds ?? 10),
    Number(config.wechat_native_file_verify_timeout_seconds ?? 45),
  ]);
}

function driverProbePayloadFingerprint(payload) {
  const probe = payload?.probe;
  if (!probe || typeof probe !== "object" || !("configured_driver" in probe)) return "";
  return JSON.stringify([
    String(probe.configured_driver || ""),
    configBoolean(probe.send_enabled, false),
    selectedSendBackend(probe.send_backend),
    String(probe.weflow_base_url || ""),
    String(probe.weflow_token_env || ""),
    String(probe.weflow_send_text_path || ""),
    String(probe.weflow_send_file_path || ""),
    Number(probe.weflow_send_timeout_seconds ?? 35),
    String(probe.wechat_native_base_url || ""),
    String(probe.wechat_native_send_text_path || ""),
    String(probe.wechat_native_send_image_path || ""),
    String(probe.wechat_native_send_file_path || ""),
    String(probe.wechat_native_status_path || ""),
    Number(probe.wechat_native_timeout_seconds ?? 15),
    Number(probe.wechat_native_verify_timeout_seconds ?? 10),
    Number(probe.wechat_native_file_verify_timeout_seconds ?? 45),
  ]);
}

function currentDriverProbeOverlay(data = state.data, now = Date.now()) {
  const overlay = state.driverProbeOverlay;
  if (!overlay || state.controlsDirty || state.controlsSaving) return null;
  if (overlay.requestEpoch !== state.driverProbeRequestEpoch) return null;
  if (overlay.expiresAt <= now) return null;
  if (overlay.configFingerprint !== driverProbeConfigFingerprint(data)) return null;
  return overlay;
}

function invalidateDriverProbeOverlay() {
  state.driverProbeRequestEpoch += 1;
  state.driverProbeOverlay = null;
}

function invalidateWechatProbeOverlay() {
  state.wechatProbeRequestEpoch += 1;
  state.wechatProbeOverlay = null;
}

function effectiveWechatProbe(data = state.data) {
  const overlay = state.wechatProbeOverlay;
  if (overlay && overlay.requestEpoch === state.wechatProbeRequestEpoch) return overlay.payload || {};
  return data?.wechat_window_probe || {};
}

function driverNames(data) {
  const registered = data.driver_probe?.registered_send_drivers || [];
  const names = registered.map((item) => item.name).filter(Boolean);
  const configured = data.config?.send_driver;
  if (configured && !names.includes(configured)) names.unshift(configured);
  return names.length ? names : ["bridge_outbox", "not_implemented"];
}

function renderSendControlSummary(data = state.data) {
  const config = data?.config || {};
  const activeMode = $("#sendModeSegment button.active");
  const mode = activeMode ? currentMode("") : normalizeSendMode(config.mode, config.mode ? "" : "dry_run");
  const sendEnabledInput = $("#sendEnabled");
  const sendEnabled = sendEnabledInput ? sendEnabledInput.checked : configBoolean(config.send_enabled, false);
  const driverSelect = $("#driverSelect");
  const driver = String((driverSelect && driverSelect.value) || config.send_driver || "bridge_outbox").trim();
  const backendSelect = $("#sendBackendSelect");
  const backend = selectedSendBackend((backendSelect && backendSelect.value) || config.send_backend);
  const isDirty = Boolean(state.controlsDirty || state.controlsSaving);
  setSendStatusPill("#sendModeSummary", `模式：${sendModeText(mode)}`, sendModeTone(mode));
  setSendStatusPill(
    "#sendRealSendSummary",
    `真实发送：${sendEnabled ? "开启" : "关闭"}`,
    sendEnabled ? (mode === "auto" ? "danger" : "warn") : "muted",
  );
  setSendStatusPill("#sendDriverSummary", `驱动：${sendDriverText(driver)} / 后端：${sendBackendText(backend)}`, sendDriverTone(driver, data, backend));
  if (isDirty) {
    setSendStatusPill("#sendBridgeSummary", "非前台桥：保存后审查", "warn");
    return;
  }
  const explicitProbe = currentDriverProbeOverlay(data);
  if (explicitProbe) {
    const summary = driverProbeSummary(explicitProbe.payload);
    setSendStatusPill("#sendBridgeSummary", summary.text, summary.tone);
    return;
  }
  const bridgeStatus = data?.capture?.background_send_status || "bridge_outbox_available";
  setSendStatusPill("#sendBridgeSummary", `非前台桥：${backgroundSendText(bridgeStatus)}`, bridgeStatusTone(bridgeStatus));
}

function driverProbeSummary(payload) {
  const probe = payload?.probe?.driver_probe || {};
  const blockers = Array.isArray(probe.blockers) ? probe.blockers.map((item) => String(item || "")).filter(Boolean) : [];
  if (String(probe.health || "") === "ready" && !blockers.length) {
    return { text: "后端探测：已就绪（60 秒内）", tone: "ok" };
  }
  if (!Object.keys(probe).length) {
    return { text: "后端探测：驱动未提供探测结果", tone: "warn" };
  }
  const labels = {
    send_enabled_false: "真实发送未启用",
    weflow_http_unavailable: "WeFlow HTTP 不可达",
    weflow_http_token_missing: "WeFlow token 缺失",
    wechat_native_http_unavailable: "Native HTTP 不可达",
    bridge_worker_config_stale: "worker 配置已过期",
    bridge_worker_config_unknown: "worker 配置未知",
  };
  const detail = blockers.map((item) => labels[item] || item).join("、") || String(probe.health || "未就绪");
  const onlyDisabled = blockers.length === 1 && blockers[0] === "send_enabled_false";
  return { text: `后端探测：${detail}`, tone: onlyDisabled ? "muted" : "danger" };
}

function setSendStatusPill(selector, text, tone) {
  const node = $(selector);
  if (!node) return;
  node.textContent = text;
  node.title = text;
  node.className = `send-status-pill is-${tone || "muted"}`;
}

function sendModeText(mode) {
  const normalized = normalizeSendMode(mode, "");
  return {
    dry_run: "演练",
    confirm: "审核",
    auto: "自动",
  }[normalized] || "未识别";
}

function sendModeTone(mode) {
  const normalized = normalizeSendMode(mode, "");
  return {
    dry_run: "muted",
    confirm: "warn",
    auto: "danger",
  }[normalized] || "danger";
}

function sendDriverText(driver) {
  if (!driver || driver === "not_implemented") return "未接入";
  if (driver === "bridge_outbox") return "非前台桥";
  return driver;
}

function sendDriverTone(driver, data = state.data, backend = "") {
  if (!driver || driver === "not_implemented") return "danger";
  const selectedBackend = selectedSendBackend(backend || data?.config?.send_backend);
  if (driver === "bridge_outbox" && !["weflow_http", "wechat_native_http"].includes(selectedBackend)) return "warn";
  const registered = new Set((data?.driver_probe?.registered_send_drivers || []).map((item) => String(item.name || "")));
  if (!registered.size) return driver === "bridge_outbox" ? "ok" : "warn";
  return registered.has(driver) ? "ok" : "danger";
}

function sendBackendText(backend) {
  return {
    dry_run: "dry-run演练",
    weflow_http: "WeFlow HTTP真实",
    wechat_native_http: "本地微信 Native HTTP",
  }[selectedSendBackend(backend)] || "dry-run演练";
}

function bridgeStatusTone(status) {
  return {
    bridge_outbox_available: "muted",
    bridge_outbox_configured_disabled: "muted",
    bridge_outbox_dry_run_backend: "warn",
    bridge_outbox_weflow_http_unavailable: "danger",
    bridge_outbox_weflow_token_missing: "danger",
    bridge_outbox_weflow_send_not_supported: "danger",
    bridge_outbox_wechat_native_http_unavailable: "danger",
    bridge_outbox_wechat_native_accepted_unverified: "warn",
    bridge_outbox_ready: "ok",
    bridge_outbox_worker_down: "warn",
    bridge_outbox_worker_down_backlog: "danger",
    bridge_outbox_backend_probe_deferred: "warn",
    bridge_outbox_worker_config_unknown: "warn",
    bridge_outbox_worker_stale_config: "danger",
  }[status] || "muted";
}

function renderWechatProbe(probe) {
  const active = probe.active || {};
  const windows = probe.windows || [];
  const first = windows[0] || {};
  const status = active.status || probe.status || "unknown";
  const unchecked = status === "unchecked";
  $("#diagnosticDetail").textContent = [
    active.title || first.title || (unchecked ? "尚未主动探测" : "未发现可用微信聊天窗口"),
    probeStatusText(status),
    first.process_name || "",
    first.hwnd ? `hwnd ${first.hwnd}` : (unchecked ? "" : "仅诊断"),
    unchecked ? "" : (probe.ui_automation?.available ? "UIA 可用" : (probe.ui_automation?.reason || "UIA 未知")),
  ].filter(Boolean).join(" / ");

  const list = $("#handleList");
  list.innerHTML = "";
  if (!windows.length) {
    list.append(emptyNode(unchecked ? "尚未主动探测微信窗口" : "没有发现可用的微信窗口句柄"));
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
    requestAnimationFrame(() => {
      if (node.isConnected) node.dataset.userToggleArmed = "1";
    });
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
      <span>已隐藏 ${channels.hidden_count} 个未信任或异常通道：${escapeHtml(reasonSummary(channels.hidden_reasons || {}))}</span>
      <button class="ghost small" type="button">清理隐藏通道</button>
    `;
    note.querySelector("button").addEventListener("click", (event) =>
      runTask(
        {
          label: "清理隐藏通道",
          category: "通道",
          scope: "channels:cleanup",
          scopeLabel: "通道维护串行",
          button: event.currentTarget,
        },
        () => cleanupHiddenChannels(),
      ),
    );
    list.append(note);
  }
  if (!items.length) {
    state.renderingChannelLanes = false;
    list.append(emptyNode("还没有可信后端服务通道"));
    return;
  }
  for (const channel of items.slice(0, 8)) {
    const displayName = channelDisplayName(channel);
    const node = document.createElement("article");
    node.className = "channel-item";
    node.innerHTML = `
      <div class="channel-main">
        <strong>${escapeHtml(displayName)}</strong>
        <span>${escapeHtml(conversationTypeText(channel.conversation_type || ""))}</span>
      </div>
      <p>${escapeHtml(channelDisplayHint(channel))}</p>
      <div class="channel-meta">
        <span>key 槽 ${(channel.api_key_refs || []).length || channel.key_slots || 0}</span>
        <span>${escapeHtml(channel.session_scope || "独立 session")}</span>
        <span>${escapeHtml(shortTime(channel.updated_at || ""))}</span>
      </div>
      <div class="channel-actions"></div>
    `;
    const actions = node.querySelector(".channel-actions");
    actions.append(actionButton("测文本", "ghost small", (helpers) => queueChannelTestReply(channel.conversation_id, helpers), {
      label: `生成测试回复：${displayName}`,
      category: "发送测试",
      scope: `conversation:${channel.conversation_id}`,
      scopeLabel: "通道发送探针",
      target: channel.conversation_id,
      persist: false,
    }));
    actions.append(actionButton("测文件", "ghost small", (helpers) => queueChannelTestFile(channel.conversation_id, helpers), {
      label: `投递测试文件：${displayName}`,
      category: "发送测试",
      scope: `conversation:${channel.conversation_id}`,
      scopeLabel: "通道文件探针",
      target: channel.conversation_id,
      persist: false,
    }));
    actions.append(actionButton("清除通道", "ghost small", () => deleteChannel(channel.conversation_id), {
      label: `清除通道：${displayName}`,
      category: "通道",
      scope: `channels:delete:${channel.conversation_id}`,
      scopeLabel: "通道维护事件",
      target: channel.conversation_id,
    }));
    list.append(node);
  }
}

function renderCounts(data) {
  $("#pendingCount").textContent = data.queues?.pending?.count || 0;
  $("#approvedCount").textContent = data.queues?.approved?.count || 0;
  $("#bridgeQueuedCount").textContent = data.queues?.queued_to_bridge?.count || 0;
  $("#acceptedCount").textContent = data.queues?.accepted?.count || 0;
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
  const channelGroups = Array.isArray(queue.channels)
    ? queue.channels.filter((item) => Array.isArray(item.items) && item.items.length)
    : [];
  if (channelGroups.length) {
    for (const channel of channelGroups) {
      list.append(renderQueueChannelGroup(channel));
    }
    return;
  }
  for (const item of queue.items) {
    list.append(renderQueueItem(item));
  }
}

function renderQueueChannelGroup(channel) {
  const details = document.createElement("details");
  details.className = "queue-channel";
  details.open = true;
  const conversationId = String(channel.conversation_id || "");
  const summary = document.createElement("summary");
  summary.innerHTML = `
    <div class="queue-channel-head">
      <div>
        <strong>${escapeHtml(channel.display_name || conversationLabel(conversationId) || "未知通道")}</strong>
        <span>${escapeHtml([
          conversationTypeText(channel.conversation_type || ""),
          channel.receiver ? `receiver ${channel.receiver}` : "",
          conversationId && channel.display_name !== conversationId ? conversationId : "",
        ].filter(Boolean).join(" / "))}</span>
      </div>
      <em>${escapeHtml(`${channel.count || channel.items.length} 条`)}</em>
    </div>
    <div class="queue-channel-status">${escapeHtml(queueChannelStatusSummary(channel.status_counts || {}))}</div>
  `;
  details.append(summary);
  const body = document.createElement("div");
  body.className = "queue-channel-items";
  for (const item of channel.items || []) {
    body.append(renderQueueItem(item));
  }
  details.append(body);
  return details;
}

function renderQueueItem(item) {
  const reply = item.reply || {};
  const note = String(item.note || item.reason || "");
  const node = document.createElement("article");
  node.className = `queue-item status-${item.status || state.activeStatus}`;
  node.innerHTML = `
    <div class="queue-head">
      <span>${escapeHtml(queueStatusText(item.status || state.activeStatus, note))}</span>
      <time>${escapeHtml(shortTime(item.updated_at || item.reviewed_at || reply.created_at || ""))}</time>
    </div>
    <div class="conversation">${escapeHtml(conversationLabel(reply.conversation_id || item.conversation_id || ""))}</div>
    <div class="reply-text">${escapeHtml(reply.text || "")}</div>
    ${note ? `<p>${escapeHtml(note)}</p>` : ""}
    <div class="actions"></div>
  `;
  const actions = node.querySelector(".actions");
  const conversationId = reply.conversation_id || item.conversation_id || "";
  const queueScope = `queue:${item.queue_id}`;
  if (item.status === "pending") {
    actions.append(actionButton("通过", "primary", () => queueAction(item.queue_id, "approve"), {
      label: `通过回复：${conversationId || item.queue_id}`,
      category: "发送审核",
      scope: queueScope,
      scopeLabel: "审核队列事件",
      target: item.queue_id,
    }));
    actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject"), {
      label: `拒绝回复：${conversationId || item.queue_id}`,
      category: "发送审核",
      scope: queueScope,
      scopeLabel: "审核队列事件",
      target: item.queue_id,
    }));
  }
  if (item.status === "approved") {
    actions.append(actionButton("发送", "primary", () => queueAction(item.queue_id, "send-approved"), {
      label: `发送回复：${conversationId || item.queue_id}`,
      category: "发送审核",
      scope: queueScope,
      scopeLabel: "审核队列事件",
      target: item.queue_id,
    }));
    actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject"), {
      label: `拒绝回复：${conversationId || item.queue_id}`,
      category: "发送审核",
      scope: queueScope,
      scopeLabel: "审核队列事件",
      target: item.queue_id,
    }));
  }
  if (["failed", "rejected", "sent", "accepted"].includes(item.status || state.activeStatus)) {
    actions.append(actionButton("×", "danger mini", () => removeQueueItem(item.queue_id), {
      label: `移除队列项：${conversationId || item.queue_id}`,
      category: "发送审核",
      scope: queueScope,
      scopeLabel: "审核队列事件",
      target: item.queue_id,
    }));
  }
  return node;
}

function queueChannelStatusSummary(counts) {
  const parts = ["pending", "approved", "queued_to_bridge", "accepted", "rejected", "sent", "failed"]
    .map((status) => [queueStatusText(status), Number(counts[status] || 0)])
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`);
  return parts.join(" / ") || "无待处理项";
}

function renderBridge(bridge) {
  const openProblems = Number(
    bridge.active_problem_count !== undefined ? bridge.active_problem_count : bridge.open_problem_count || 0,
  );
  const activeUnverified = Number(
    bridge.active_unverified_count !== undefined
      ? bridge.active_unverified_count
      : bridge.accepted_count || bridge.unverified_count || 0,
  );
  const legacyUnverified = Number(bridge.legacy_hook_unverified_count || 0);
  const historicalFailures = Number(
    bridge.historical_failed_count !== undefined ? bridge.historical_failed_count : bridge.failed_count || 0,
  );
  const summaryParts = [
    `${openProblems} 当前需复核`,
    `${bridge.pending_count || 0} 待消费`,
    `${bridge.sent_count || 0} 已送达`,
    `${activeUnverified} 当前未验证`,
    `${historicalFailures} 历史失败`,
    `${bridge.blocked_count || 0} 阻断`,
    `${bridge.channel_count || 0} 通道`,
  ];
  if (legacyUnverified) {
    summaryParts.splice(4, 0, `${legacyUnverified} 旧端口未验证`);
  }
  $("#bridgePendingCount").textContent = summaryParts.join(" / ");
  $("#bridgePath").textContent = [
    bridge.outbox_path || "outbox not created",
    bridgeWorkerSummary(bridge.worker || {}),
  ].filter(Boolean).join(" | ");
  const list = $("#bridgeList");
  list.innerHTML = "";
  const channels = Array.isArray(bridge.channels) ? bridge.channels : [];
  if (channels.length) {
    const channelBox = document.createElement("section");
    channelBox.className = "bridge-channel-list";
    for (const channel of channels) {
      const node = document.createElement("article");
      node.className = "bridge-channel-item";
      const conversationId = String(channel.conversation_id || "");
      node.innerHTML = `
        <div>
          <strong>${escapeHtml(channel.display_name || conversationLabel(conversationId))}</strong>
          <span>${escapeHtml(conversationTypeText(channel.conversation_type || ""))} / ${escapeHtml(channel.bridge_ready ? `receiver ${channel.receiver}` : "缺少 receiver")}</span>
        </div>
      `;
      if (conversationId) {
        node.append(actionButton("×", "danger mini", () => deleteChannel(conversationId), {
          label: `删除桥通道：${channel.display_name || conversationId}`,
          category: "通道",
          scope: `channels:delete:${conversationId}`,
          scopeLabel: "通道维护事件",
          target: conversationId,
        }));
      }
      channelBox.append(node);
    }
    list.append(channelBox);
  }
  const items = bridge.items || [];
  if (!items.length) {
    list.append(emptyNode("非前台桥 outbox 为空"));
    return;
  }
  const itemChannels = Array.isArray(bridge.item_channels)
    ? bridge.item_channels.filter((item) => Array.isArray(item.items) && item.items.length)
    : [];
  if (itemChannels.length) {
    for (const channel of itemChannels) {
      list.append(renderBridgeChannelGroup(channel));
    }
    return;
  }
  for (const item of items.slice(-10).reverse()) {
    list.append(renderBridgeItem(item));
  }
}

function renderBridgeChannelGroup(channel) {
  const details = document.createElement("details");
  details.className = "bridge-item-channel";
  details.open = true;
  const conversationId = String(channel.conversation_id || "");
  const summary = document.createElement("summary");
  summary.innerHTML = `
    <div class="queue-channel-head">
      <div>
        <strong>${escapeHtml(channel.display_name || conversationLabel(conversationId) || "未知通道")}</strong>
        <span>${escapeHtml([
          conversationTypeText(channel.conversation_type || ""),
          channel.receiver ? `receiver ${channel.receiver}` : "",
          conversationId && channel.display_name !== conversationId ? conversationId : "",
        ].filter(Boolean).join(" / "))}</span>
      </div>
      <em>${escapeHtml(`${channel.count || channel.items.length} 条`)}</em>
    </div>
    <div class="queue-channel-status">${escapeHtml(bridgeChannelStatusSummary(channel.status_counts || {}))}</div>
  `;
  details.append(summary);
  const body = document.createElement("div");
  body.className = "queue-channel-items";
  for (const item of channel.items || []) {
    body.append(renderBridgeItem(item));
  }
  details.append(body);
  return details;
}

function renderBridgeItem(item) {
  const node = document.createElement("article");
  node.className = `bridge-item status-${item.status || "queued"}`;
  const ack = item.ack && typeof item.ack === "object" ? item.ack : {};
  const ackReason = String(ack.reason || "");
  const ackBackend = String(item.ack_backend || "");
  const retryBlocker = String(item.retry_blocker || "");
  node.innerHTML = `
    <div class="queue-head">
      <span>${escapeHtml(bridgeStatusText(item.status || "queued", ackReason))}</span>
      <time>${escapeHtml(shortTime(item.created_at || ""))}</time>
    </div>
    <div class="conversation">${escapeHtml(conversationLabel(item.conversation_id || ""))}</div>
    <div class="reply-text">${escapeHtml(item.text || "")}</div>
    <p>${escapeHtml([item.bridge_id || "", item.receiver ? `receiver=${item.receiver}` : "", ackBackend ? `backend=${ackBackend}` : "", ackReason, retryBlocker].filter(Boolean).join(" / "))}</p>
    <div class="actions"></div>
  `;
  const actions = node.querySelector(".actions");
  if (item.retryable) {
    actions.append(actionButton("重投", "primary", () => retryBridge(item.bridge_id), {
      label: `重投桥接项：${item.conversation_id || item.bridge_id}`,
      category: "非前台桥",
      scope: `send-review:bridge:${item.bridge_id || item.conversation_id}`,
      scopeLabel: "非前台桥重投",
      target: item.conversation_id || "",
    }));
  }
  if ((item.status || "queued") === "queued") {
    actions.append(actionButton("标记已发", "primary", () => ackBridge(item.bridge_id, "sent"), {
      label: `桥接标记已发：${item.conversation_id || item.bridge_id}`,
      category: "非前台桥",
      scope: `send-review:bridge:${item.bridge_id || item.conversation_id}`,
      scopeLabel: "非前台桥事件",
      target: item.conversation_id || "",
    }));
    actions.append(actionButton("标记失败", "danger", () => ackBridge(item.bridge_id, "failed"), {
      label: `桥接标记失败：${item.conversation_id || item.bridge_id}`,
      category: "非前台桥",
      scope: `send-review:bridge:${item.bridge_id || item.conversation_id}`,
      scopeLabel: "非前台桥事件",
      target: item.conversation_id || "",
    }));
  }
  return node;
}

function bridgeChannelStatusSummary(counts) {
  const parts = ["queued", "inflight", "accepted", "sent", "failed", "blocked", "retry"]
    .map((status) => [bridgeStatusText(status), Number(counts[status] || 0)])
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`);
  return parts.join(" / ") || "无桥接项";
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
  renderChannelRuntimeOverrides(runtimeCards, catalog);
  renderCardList({
    root: $("#skillList"),
    cards: catalog.filter((item) => item.card_type === "skill"),
    activeIds: activeSkillIds,
    emptyText: "暂无技能卡",
    actionFor: (card, activeNow) =>
      actionButton(activeNow ? "停用" : "启用", activeNow ? "ghost small" : "primary small", () =>
        runtimeCardAction(activeNow ? "disable-skill" : "enable-skill", { card_id: card.card_id }),
        {
          label: `${activeNow ? "停用" : "启用"}技能：${card.name || card.card_id}`,
          category: "技能/人设",
          scope: "settings:runtime-cards",
        },
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
        {
          label: `${activeNow ? "查看已装备" : "装备"}人物卡：${card.name || card.card_id}`,
          category: "技能/人设",
          scope: "settings:runtime-cards",
        },
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
        {
          label: `${activeNow ? "卸载" : "装备"}任务卡：${card.name || card.card_id}`,
          category: "技能/人设",
          scope: "settings:runtime-cards",
        },
      ),
  });
}

function renderChannelRuntimeOverrides(runtimeCards, catalog) {
  const overrides = runtimeCards.state?.channel_overrides || {};
  $("#channelOverrideCount").textContent = `${Object.keys(overrides).length} 覆盖`;
  renderChannelOverrideOptions();
  const personaSelect = $("#channelOverridePersonaSelect");
  if (personaSelect) {
    const current = personaSelect.value;
    const personas = catalog.filter((item) => item.card_type === "persona");
    personaSelect.innerHTML = [
      `<option value="">继承全局人物卡</option>`,
      ...personas.map((card) => `<option value="${escapeHtml(card.card_id)}">${escapeHtml(card.name || card.card_id)}</option>`),
    ].join("");
    if ([...personaSelect.options].some((option) => option.value === current)) {
      personaSelect.value = current;
    }
  }
  const skillInput = $("#channelOverrideSkillIds");
  if (skillInput && !skillInput.dataset.placeholderBound) {
    const skills = catalog.filter((item) => item.card_type === "skill").map((item) => item.card_id).join(", ");
    if (skills) skillInput.placeholder = `技能 card_id，逗号分隔；可用：${skills}`;
    skillInput.dataset.placeholderBound = "1";
  }
  renderChannelOverrideList(overrides, catalog);
}

function renderChannelOverrideOptions() {
  const options = $("#channelOverrideConversationOptions");
  if (!options) return;
  const channels = Array.isArray(state.data?.channels?.items) ? state.data.channels.items : [];
  options.innerHTML = channels
    .filter((channel) => String(channel?.conversation_id || "").trim())
    .map((channel) => {
      const id = String(channel.conversation_id || "").trim();
      const label = [channelDisplayName(channel), conversationTypeText(channel.conversation_type || "")]
        .filter(Boolean)
        .join(" / ");
      return `<option value="${escapeHtml(id)}" label="${escapeHtml(label || id)}"></option>`;
    })
    .join("");
}

function renderChannelOverrideList(overrides, catalog) {
  const list = $("#channelOverrideList");
  if (!list) return;
  list.innerHTML = "";
  const entries = Object.entries(overrides || {}).filter(([conversationId]) => String(conversationId || "").trim());
  if (!entries.length) {
    list.append(emptyNode("暂无通道人设覆盖"));
    return;
  }
  const byId = new Map(catalog.map((card) => [card.card_id, card]));
  for (const [conversationId, override] of entries) {
    const payload = override && typeof override === "object" ? override : {};
    const personaId = String(payload.persona_id || "").trim();
    const skillIds = Array.isArray(payload.skill_ids) ? payload.skill_ids.map((item) => String(item || "").trim()).filter(Boolean) : [];
    const persona = personaId ? byId.get(personaId) : null;
    const skillNames = skillIds.map((id) => byId.get(id)?.name || id);
    const node = document.createElement("article");
    node.className = "runtime-card channel-override-card";
    node.innerHTML = `
      <div class="runtime-card-head">
        <div>
          <strong>${escapeHtml(conversationLabel(conversationId) || conversationId)}</strong>
          <span>${escapeHtml(conversationId)}</span>
        </div>
        <span class="card-state">通道覆盖</span>
      </div>
      <p>${escapeHtml([
        persona ? `人物卡：${persona.name || persona.card_id}` : "人物卡：继承全局",
        skillNames.length ? `技能：${skillNames.join(", ")}` : "技能：无通道覆盖",
      ].join(" / "))}</p>
      <div class="actions"></div>
    `;
    node.querySelector(".actions").append(simpleButton("编辑", "ghost small", () => {
      $("#channelOverrideConversationId").value = conversationId;
      const personaSelect = $("#channelOverridePersonaSelect");
      if (personaSelect) personaSelect.value = personaId;
      const skillInput = $("#channelOverrideSkillIds");
      if (skillInput) skillInput.value = skillIds.join(", ");
    }));
    list.append(node);
  }
}

function renderWeFlow(weflow) {
  const worker = weflow.worker || {};
  const pullJob = weflow.pull_job || {};
  const backfillJob = weflow.backfill_job || {};
  const metrics = worker.metrics || {};
  const bridgeState = weflow.bridge_state || {};
  const readiness = weflow.readiness || {};
  const requestedTalkers = Array.isArray(weflow.requested_talkers) ? weflow.requested_talkers : [];
  const selectedTalkers = talkerIds();
  $("#weflowStatus").textContent = worker.running
    ? `后台运行中 / ${worker.loops || 0} 轮`
    : weflowReadinessText(readiness.status || weflow.last_pull?.status || weflow.last_health?.status || "unchecked");
  $("#weflowDetail").textContent = [
    selectedTalkers.length ? `前端选中 ${selectedTalkers.length} talker` : "",
    requestedTalkers.length ? `后端最近请求 ${requestedTalkers.length} talker` : "",
    readiness.token_present ? `token ${readiness.token_source === "environment" ? "存在（环境变量）" : "存在"}` : "token 缺失",
    readiness.service_reachable ? "WeFlow 可达" : "",
    readiness.status === "health_stale" ? "Health 已过期，请重新检查" : "",
    readiness.fork_ok ? "fork marker 正常" : "",
    weflow.security?.primary_source || "weflow_local_fork",
    weflow.security?.requires_token_for_pull ? "正式拉取需要 token" : "",
    weflow.security?.requires_local_fork_marker ? "需要本地 fork marker" : "",
    metrics.stalled ? "⚠ 后台疑似停滞（长时间无成功拉取）" : "",
    worker.stop_requested ? "正在停止后台拉取" : "",
    pullJob.running ? `单次拉取后台运行 ${pullJob.seconds_running || 0}s` : "",
    worker.last_status === "restarting" ? `⚠ 后台已崩溃，自动重启中（第 ${worker.restart_count || 0} 次）` : "",
    worker.last_status === "crashed" ? `✕ 后台已崩溃并停止（重启 ${worker.restart_count || 0} 次后放弃）` : "",
    metrics.slow_ticks ? `慢 tick ${metrics.slow_ticks} 次` : "",
    bridgeState.session_count ? `会话游标 ${bridgeState.session_count} / 去重 ${bridgeState.seen_raw_id_count || 0}` : "",
    worker.last_error ? `后台错误：${worker.last_error}` : "",
  ].filter(Boolean).join(" / ");
  if (!$("#weflowBaseUrl").dataset.touched) $("#weflowBaseUrl").value = weflow.base_url || "http://127.0.0.1:5031";
  if (!$("#weflowTokenEnv").dataset.touched) $("#weflowTokenEnv").value = weflow.token_env || "WEFLOW_API_TOKEN";
  const statusPayload = {
    worker,
    readiness,
    pull_job: pullJob,
    backfill_job: backfillJob,
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
    selected_talkers: selectedTalkers,
    requested_talkers: requestedTalkers,
    last_health: weflow.last_health || {},
    last_discover: compactPayload(weflow.last_discover || {}, 1800),
    last_pull: compactPayload(weflow.last_pull || {}, 1800),
    last_backfill: compactPayload(weflow.last_backfill || {}, 1800),
    operation_history: compactPayload(weflow.operation_history || [], 1800),
    files: {
      hook_event_file: weflow.hook_event_file,
      backend_event_file: weflow.backend_event_file,
      weflow_state_file: weflow.weflow_state_file,
    },
    native_migration: state.data?.native_migration || {},
  };
  state.weflowLatestStatusText = JSON.stringify(statusPayload, null, 2);
  if (state.weflowStatusMode === "live" || !$("#weflowStatusBox").textContent.trim()) {
    showWeFlowStatusText(state.weflowLatestStatusText, "live");
  }
  const cancelBackfillButton = $("#weflowCancelBackfillButton");
  if (cancelBackfillButton && !cancelBackfillButton.dataset.taskLocked) {
    cancelBackfillButton.disabled = !(backfillJob.running || backfillJob.status === "cancel_requested");
  }
  const backfillButton = $("#weflowBackfillButton");
  if (backfillButton && !backfillButton.dataset.taskLocked) {
    backfillButton.disabled = Boolean(backfillJob.running || backfillJob.status === "cancel_requested");
  }
  const pullButton = $("#weflowPullButton");
  if (pullButton && !pullButton.dataset.taskLocked) {
    pullButton.disabled = Boolean(pullJob.running);
  }
  const startButton = $("#weflowStartButton");
  if (startButton && !startButton.dataset.taskLocked) {
    startButton.disabled = Boolean(worker.running);
  }
  const stopButton = $("#weflowStopButton");
  if (stopButton && !stopButton.dataset.taskLocked) {
    stopButton.disabled = !Boolean(worker.running || worker.stop_requested);
  }
  const envLocked = Boolean(worker.running || pullJob.running || backfillJob.running || backfillJob.status === "cancel_requested");
  for (const selector of ["#weflowDepsButton", "#weflowInstallButton"]) {
    const button = $(selector);
    if (button && !button.dataset.taskLocked) {
      button.disabled = envLocked;
    }
  }
  syncBackfillTask(backfillJob);
  renderWeFlowStoredSessions(weflow.discovered_sessions?.sessions || []);
  renderTalkerChips();
  renderWeFlowHistory(weflow.operation_history || []);
}

function renderWeFlowStoredSessions(sessions) {
  const list = $("#weflowStoredSessionList");
  const count = $("#weflowStoredSessionCount");
  if (!list || !count) return;
  const items = Array.isArray(sessions) ? sessions : [];
  const filter = String($("#weflowStoredSessionFilter")?.value || "").trim().toLowerCase();
  const filtered = filter
    ? items.filter((session) =>
        [session.id, session.name, session.conversation_id]
          .map((value) => String(value || "").toLowerCase())
          .some((value) => value.includes(filter)),
      )
    : items;
  count.textContent = `${filtered.length}/${items.length} 个`;
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyNode("本地库为空，先发现一次会话"));
    return;
  }
  if (!filtered.length) {
    list.append(emptyNode("没有匹配的本地通道"));
    return;
  }
  const selected = new Set(talkerIds());
  for (const session of filtered.slice(0, 20)) {
    const sessionId = String(session.id || "").trim();
    if (!sessionId) continue;
    const blocked = isBlockedWeFlowSession(session);
    const node = document.createElement("article");
    node.className = `stored-session-item${blocked ? " blocked" : ""}`;
    node.innerHTML = `
      <div>
        <strong>${escapeHtml(session.name || sessionId)}</strong>
        <span>${escapeHtml(conversationTypeText(session.type || ""))} / ${escapeHtml(session.cached ? "本地库" : "实时发现")}${blocked ? ` / ${escapeHtml(weflowBlockedSessionText(session))}` : ""}</span>
      </div>
      <div class="stored-session-actions"></div>
    `;
    const actions = node.querySelector(".stored-session-actions");
    const addButton = simpleButton(selected.has(sessionId) ? "已加入" : (blocked ? "已阻断" : "加入"), "ghost mini", () => addTalker(sessionId));
    if (blocked) {
      addButton.disabled = true;
      addButton.title = weflowBlockedSessionText(session);
    }
    actions.append(addButton);
    if (session.conversation_id) {
      actions.append(actionButton("×", "danger mini", () => deleteChannel(session.conversation_id), {
        label: `删除本地通道：${session.name || sessionId}`,
        category: "通道",
        scope: `channels:delete:${session.conversation_id}`,
        scopeLabel: "通道维护事件",
        target: session.conversation_id,
      }));
    }
    list.append(node);
  }
}

function renderTalkerChips() {
  const list = $("#weflowTalkerChips");
  if (!list) return;
  const ids = talkerIds();
  list.hidden = !ids.length;
  list.innerHTML = "";
  for (const id of ids) {
    const node = document.createElement("div");
    node.className = "talker-chip";
    node.innerHTML = `
      <div>
        <strong>${escapeHtml(talkerDisplayName(id))}</strong>
        <span>${escapeHtml(id)}</span>
      </div>
    `;
    node.append(simpleButton("×", "ghost mini", () => removeTalker(id)));
    list.append(node);
  }
}

function talkerIds() {
  return splitComma($("#weflowTalkers")?.value || "");
}

function setTalkerIds(ids) {
  const input = $("#weflowTalkers");
  if (!input) return;
  input.value = [...new Set((ids || []).map((item) => String(item || "").trim()).filter(Boolean))].join(", ");
  renderTalkerChips();
  renderWeFlowStoredSessions(state.data?.weflow?.discovered_sessions?.sessions || []);
}

function addTalker(id) {
  const sessionId = String(id || "").trim();
  if (!sessionId) return;
  setTalkerIds([...talkerIds(), sessionId]);
  setStatusMessage(`已添加会话：${sessionId}`);
}

function removeTalker(id) {
  const sessionId = String(id || "").trim();
  setTalkerIds(talkerIds().filter((item) => item !== sessionId));
  setStatusMessage(`已移除会话：${sessionId}`);
}

function talkerDisplayName(id) {
  const sessionId = String(id || "").trim();
  const sources = [
    ...(state.data?.weflow?.discovered_sessions?.sessions || []),
    ...(state.data?.weflow?.last_discover?.sessions || []),
  ];
  const match = sources.find((item) => String(item?.id || "").trim() === sessionId);
  return match?.name || sessionId;
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
  const audit = state.data?.audit || {};
  const items = audit.items || [];
  $("#auditCount").textContent = items.length;
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyNode("暂无发送审计记录"));
    return;
  }
  const channelGroups = Array.isArray(audit.channels)
    ? audit.channels.filter((item) => Number(item.total_count || 0) > 0)
    : [];
  if (channelGroups.length) {
    for (const channel of channelGroups) {
      list.append(renderAuditChannelGroup(channel));
    }
    return;
  }
  for (const item of items.slice(-8).reverse()) {
    list.append(renderAuditItem(item));
  }
}

function renderAuditChannelGroup(channel) {
  const details = document.createElement("details");
  details.className = "audit-channel queue-channel";
  details.open = true;
  const conversationId = String(channel.conversation_id || "");
  const summary = document.createElement("summary");
  summary.innerHTML = `
    <div class="queue-channel-head">
      <div>
        <strong>${escapeHtml(channel.display_name || conversationLabel(conversationId) || "未知通道")}</strong>
        <span>${escapeHtml([
          conversationTypeText(channel.conversation_type || ""),
          channel.receiver ? `receiver ${channel.receiver}` : "",
          conversationId && channel.display_name !== conversationId ? conversationId : "",
        ].filter(Boolean).join(" / "))}</span>
      </div>
      <em>${escapeHtml(`${channel.total_count || 0} 条`)}</em>
    </div>
    <div class="queue-channel-status">${escapeHtml(auditChannelPhaseSummary(channel.phase_counts || {}))}</div>
  `;
  details.append(summary);
  const body = document.createElement("div");
  body.className = "queue-channel-items";
  for (const phase of auditPhaseOrder()) {
    const bucket = channel.phases?.[phase] || {};
    const phaseItems = Array.isArray(bucket.items) ? bucket.items : [];
    if (!phaseItems.length) continue;
    body.append(renderAuditPhaseGroup(phase, phaseItems));
  }
  details.append(body);
  return details;
}

function renderAuditPhaseGroup(phase, items) {
  const group = document.createElement("section");
  group.className = "audit-phase-group";
  const recent = [...items]
    .sort((a, b) => String(a.timestamp || "").localeCompare(String(b.timestamp || "")))
    .slice(-8)
    .reverse();
  const head = document.createElement("div");
  head.className = "audit-phase-head";
  head.innerHTML = `
    <strong>${escapeHtml(auditPhaseText(phase))}</strong>
    <span>${escapeHtml(`${items.length} 条`)}</span>
  `;
  group.append(head);
  const body = document.createElement("div");
  body.className = "audit-phase-items";
  for (const item of recent) {
    body.append(renderAuditItem(item));
  }
  group.append(body);
  return group;
}

function renderAuditItem(item) {
    const node = document.createElement("article");
    const resolved = Boolean(item.resolved);
    const severity = auditSeverity(item);
    const failed = Boolean(item.problem) || (severity === "error" && !resolved);
    node.className = `audit-item audit-item-${severity}${resolved ? " audit-item-resolved" : ""}${failed ? " audit-item-error" : ""}`;
    const status = auditDisplayStatusText(item, resolved);
    const detail = auditDetailText(item, resolved);
    node.innerHTML = `
      <span>${escapeHtml(actionText(item.action || ""))}</span>
      <strong>${escapeHtml(status)}</strong>
      <p>${escapeHtml(detail)}</p>
    `;
  return node;
}

function auditChannelPhaseSummary(counts) {
  const parts = auditPhaseOrder()
    .map((phase) => [auditPhaseText(phase), Number(counts[phase] || 0)])
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`);
  return parts.join(" / ") || "无审计项";
}

function auditPhaseOrder() {
  return ["pending", "approved", "queued_to_bridge", "accepted", "rejected", "sent", "failed", "blocked", "resolved", "other"];
}

function auditPhaseText(phase) {
  return {
    blocked: "已阻断",
    resolved: "已恢复",
    other: "其他",
  }[phase] || queueStatusText(phase);
}

function auditSeverity(item) {
  const raw = String(item?.severity || "").toLowerCase();
  if (["error", "warning", "resolved", "info"].includes(raw)) return raw;
  if (item?.action === "ledger_sync_failed" && !item?.resolved) return "error";
  if (item?.action === "confirm_send_attempt" && item?.status === "failed") return "error";
  if (item?.action === "confirm_send_blocked") return "warning";
  return item?.resolved ? "resolved" : "info";
}

function auditDisplayStatusText(item, resolved = false) {
  if (resolved) return "已恢复";
  const action = String(item?.action || "");
  const status = String(item?.status || "");
  if (action === "confirm_approve") return "已通过";
  if (action === "confirm_reject") return "已拒绝";
  if (action === "confirm_remove") return "已移除";
  if (action === "bridge_retry") return "已重投";
  if (action === "ledger_sync_recovered") return "已恢复";
  if (action === "ledger_sync_failed") return "账本异常";
  if (action === "confirm_send_blocked") return "已阻断";
  return queueStatusText(status, item?.reason || "");
}

function auditDetailText(item, resolved = false) {
  const payload = item?.payload && typeof item.payload === "object" ? item.payload : {};
  const sendResult = payload.send_result && typeof payload.send_result === "object" ? payload.send_result : {};
  const parts = [];
  const base = item?.reason || item?.note || "";
  if (base) parts.push(String(base));
  const conversationId = item?.conversation_id || payload.conversation_id || sendResult.conversation_id || "";
  const channelName = item?.channel_display_name || "";
  if (conversationId) {
    const label = channelName && channelName !== conversationId
      ? `${channelName} (${conversationId})`
      : conversationLabel(conversationId);
    parts.push(`通道：${label}`);
  }
  if (item?.queue_id) parts.push(`审核项：${item.queue_id}`);
  const bridgeIds = [];
  if (Array.isArray(payload.bridge_ids)) bridgeIds.push(...payload.bridge_ids);
  if (payload.bridge_id) bridgeIds.push(payload.bridge_id);
  if (payload.ack_status || bridgeIds.length) {
    const ackText = payload.ack_status ? `桥回执：${queueStatusText(payload.ack_status)}` : "桥回执";
    parts.push([ackText, bridgeIds.length ? bridgeIds.join(", ") : ""].filter(Boolean).join(" / "));
  }
  if (resolved && item?.resolved_by) {
    parts.push(`已由 ${actionText(item.resolved_by)} 覆盖`);
  }
  if (!parts.length && item?.queue_id) parts.push(`审核项：${item.queue_id}`);
  return parts.join("\n");
}

async function clearSendAudit(helpers = {}) {
  if (!window.confirm("确定清空发送审计吗？\n\n该操作会删除控制台中的发送审计记录，但不会删除 send_bridge 证据链。")) {
    return { status: "cancelled_by_user", message: "用户取消清空发送审计" };
  }
  helpers.update?.(30, "正在清空发送审计");
  const payload = await api("/api/audit/clear", {
    method: "POST",
    body: JSON.stringify({}),
  });
  helpers.update?.(78, "发送审计已清空，正在刷新页面状态");
  setStatusMessage("发送审计已清空");
  await refresh({ force: true });
  return payload;
}

function weflowReadinessText(value) {
  return {
    ready: "已就绪",
    token_missing: "token 缺失",
    fork_marker_missing: "fork marker 缺失",
    health_stale: "Health 已过期",
    worker_running_unchecked: "后台运行中，尚未确认健康",
    error: "Health 检查失败",
    unchecked: "尚未检查",
  }[String(value || "")] || String(value || "未检查");
}

async function clearWeFlowHistory(helpers = {}) {
  if (!window.confirm("确定清空 WeFlow 操作历史吗？\n\n该操作只清除控制台操作记录，不会删除对话或 send_bridge 证据链。")) {
    return { status: "cancelled_by_user", message: "用户取消清空 WeFlow 操作历史" };
  }
  helpers.update?.(30, "正在清空 WeFlow 操作历史");
  const payload = await api("/api/weflow/clear-history", { method: "POST", body: JSON.stringify({}) });
  showWeFlowStatusPayload(payload);
  setStatusMessage("操作历史已清空");
  await refresh({ force: true });
  return payload;
}

async function clearHistoryData(helpers = {}) {
  const confirmed = window.confirm(
    "确定删除历史数据吗？\n\n将关闭 sidebar 和 WeFlow，清空对话、审核队列、文件中间层和 WeFlow 运行历史；侧边栏配置、模型配置、密钥池、技能/人设，以及已跑通的 send_bridge outbox/acks/worker 证据会保留。\n\n清理完成后不会自动重启，请手动重新打开 sidebar。",
  );
  if (!confirmed) {
    return { status: "cancelled_by_user", message: "用户取消清空历史数据" };
  }
  state.historyResetPending = true;
  invalidateWechatProbeOverlay();
  invalidateDriverProbeOverlay();
  state.dataEpoch += 1;
  syncTaskButtonLocks();
  helpers.update?.(18, "已确认，正在调度关闭清空");
  const unlockKnownFailure = async (payload, message) => {
    state.historyResetPending = false;
    syncTaskButtonLocks();
    helpers.update?.(100, message);
    setStatusMessage(message);
    await refresh({ force: true });
    return payload;
  };
  const retainUnknownOutcome = (payload, detail = "") => {
    const responseStatus = String(payload?.status || "");
    const message = [
      "历史清理请求结果未知；请勿重复清空。请手动关闭并重新打开 sidebar，重新打开前不要继续写入。",
      detail,
    ].filter(Boolean).join(" ");
    helpers.update?.(100, message);
    setStatusMessage(message);
    syncTaskButtonLocks();
    return {
      ...(payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {}),
      status: "error",
      outcome: "unknown",
      response_status: responseStatus,
      message,
    };
  };
  const explicitFailureMessage = (payload) => payload.status === "blocked"
    ? `历史清理被阻断：${payload.message || payload.reason || "仍有历史写入任务运行"}`
    : `历史清理未完整完成：${payload.error_count || 0} 项删除失败`;
  let payload;
  try {
    payload = await api("/api/history/clear", {
      method: "POST",
      body: JSON.stringify({ source: "sidebar", shutdown_processes: true }),
    });
  } catch (error) {
    const errorPayload = error?.payload;
    const errorPayloadStatus = String(errorPayload?.status || "");
    const httpStatus = Number(error?.httpStatus);
    const isProvenClientRejection = Number.isInteger(httpStatus)
      && httpStatus >= 400
      && httpStatus < 500
      && errorPayload
      && typeof errorPayload === "object"
      && !Array.isArray(errorPayload)
      && errorPayload.history_reset_not_scheduled === true;
    if (isProvenClientRejection) {
      const message = `历史清理请求未受理：${errorPayload.message || errorPayload.error || error.message || `HTTP ${httpStatus}`}`;
      return unlockKnownFailure(
        {
          ...errorPayload,
          status: "error",
          response_status: errorPayloadStatus,
          message,
        },
        message,
      );
    }
    return retainUnknownOutcome(errorPayload, error?.message || "请求连接已中断");
  }
  if (payload?.outcome_unknown === true) {
    return retainUnknownOutcome(payload, payload?.message || "服务无法核实清理进程状态");
  }
  if (payload?.status === "shutdown_scheduled") {
    helpers.update?.(96, "已调度：窗口即将关闭；完成后请手动重新打开 sidebar");
    setStatusMessage("已调度关闭清空；完成后请手动重新打开 sidebar");
    return payload;
  }
  if (["blocked", "partial_error"].includes(String(payload?.status || ""))) {
    return unlockKnownFailure(payload, explicitFailureMessage(payload));
  }
  if (payload?.status !== "ok") {
    const responseStatus = String(payload?.status || "");
    return retainUnknownOutcome(
      payload,
      responseStatus ? `服务返回未识别状态：${responseStatus}` : "服务未返回明确结果",
    );
  }
  helpers.update?.(78, "历史数据已清空，正在刷新页面状态");
  const retainedLocked = Number(payload.retained_locked_count || 0);
  setStatusMessage(
    retainedLocked
      ? `历史数据已清空：删除 ${payload.removed_count || 0} 项，保留 ${retainedLocked} 个被占用日志`
      : `历史数据已清空：删除 ${payload.removed_count || 0} 项`,
  );
  state.weflowStatusMode = "live";
  state.weflowLatestStatusText = "";
  state.historyResetPending = false;
  syncTaskButtonLocks();
  await refresh({ force: true });
  return payload;
}

async function inspectStorageStatus(helpers = {}) {
  helpers.update?.(18, "正在扫描存储边界");
  const box = $("#storageStatusBox");
  try {
    const payload = await api("/api/storage/status", {
      method: "POST",
      body: JSON.stringify({ include_sizes: true, max_entries_per_component: 5000 }),
    });
    state.data = { ...(state.data || {}), storage_migration: payload };
    const summary = storageStatusSummary(payload);
    if (box) {
      box.hidden = false;
      box.textContent = JSON.stringify(storageStatusDisplayPayload(payload), null, 2);
    }
    helpers.update?.(100, summary);
    setStatusMessage(summary);
    return payload;
  } catch (error) {
    const message = `存储边界扫描失败：${error.message}`;
    if (box) {
      box.hidden = false;
      box.textContent = JSON.stringify({ status: "error", message, response: error.payload || null }, null, 2);
    }
    setStatusMessage(message);
    return { status: "error", message, response: error.payload || null };
  }
}

function storageStatusDisplayPayload(payload) {
  return {
    schema: payload?.schema || "",
    status: payload?.status || "unknown",
    created_at: payload?.created_at || "",
    data_dir: payload?.data_dir || "",
    safe_default: payload?.safe_default || "",
    summary: payload?.summary || {},
    database_contract_summary: payload?.database_contract_summary || {},
    database_contracts: Array.isArray(payload?.database_contracts) ? payload.database_contracts : [],
    migration_boundaries: Array.isArray(payload?.migration_boundaries) ? payload.migration_boundaries : [],
    recommended_sequence: Array.isArray(payload?.recommended_sequence) ? payload.recommended_sequence : [],
    components: compactPayload(Array.isArray(payload?.items) ? payload.items : [], 5000),
  };
}

function storageStatusSummary(payload) {
  const summary = payload?.summary || {};
  const db = Number(summary.database_backed_count || 0);
  const fileTruth = Number(summary.file_truth_not_migrated_count || 0);
  const preserved = Number(summary.preserved_component_count || 0);
  const reset = Number(summary.reset_component_count || 0);
  const truncated = Number(summary.truncated_component_count || 0);
  const suffix = truncated ? ` / ${truncated} 项尺寸已截断` : "";
  return `存储边界：DB ${db} 项 / 文件权威 ${fileTruth} 项 / 清历史保留 ${preserved} 项 / 重置 ${reset} 项${suffix}`;
}

function renderProbeJson() {
  $("#probeBox").hidden = !state.probeExpanded;
  if (!state.probeExpanded) return;
  const driverOverlay = currentDriverProbeOverlay();
  $("#probeBox").textContent = JSON.stringify(
    {
      driver_probe: driverOverlay?.payload?.probe || state.data?.driver_probe,
      driver_probe_overlay: driverOverlay
        ? {
          checked_at: driverOverlay.checkedAt,
          expires_at: new Date(driverOverlay.expiresAt).toISOString(),
          config_fingerprint: driverOverlay.configFingerprint,
        }
        : null,
      wechat_window_probe: effectiveWechatProbe(),
      runtime_probe: state.data?.runtime_probe,
    },
    null,
    2,
  );
}

function runTask(meta, worker) {
  const scope = String(meta?.scope || "global");
  if (state.historyResetPending && scope !== "history:clear") {
    setStatusMessage("历史清理已开始，新的操作已停止接收");
    return Promise.resolve({ status: "blocked", message: "history_reset_pending" });
  }
  const active = activeTaskForScope(scope);
  if (active) {
    setStatusMessage(`${active.scopeLabel || "该作用域"}已有操作进行中：${active.label}`);
    updateButtonTaskProgress(active);
    return state.taskScopeChains.get(scope) || Promise.resolve({ status: "blocked", message: "task_scope_busy" });
  }
  const task = createTask(meta);
  const runner = Promise.resolve().then(() => executeTask(task, worker));
  const cleanup = runner.finally(() => {
    if (state.taskScopeChains.get(scope) === cleanup) {
      state.taskScopeChains.delete(scope);
    }
  });
  state.taskScopeChains.set(scope, cleanup);
  return cleanup;
}

function activeTaskForScope(scope) {
  return state.tasks.find((task) => task.scope === scope && ["queued", "running"].includes(task.status)) || null;
}

function createTask(meta = {}) {
  const now = new Date().toISOString();
  const task = {
    id: `task-${Date.now()}-${++state.taskSeq}`,
    label: meta.label || "未命名操作",
    category: meta.category || "操作",
    scope: meta.scope || "global",
    scopeLabel: meta.scopeLabel || taskScopeText(meta.scope || "global"),
    target: meta.target || "",
    status: "queued",
    progress: 0,
    phase: "等待执行",
    priority: Number.isFinite(Number(meta.priority)) ? Number(meta.priority) : 50,
    queuedAt: now,
    startedAt: "",
    finishedAt: "",
    updatedAt: now,
    detail: meta.detail || "",
    button: meta.button || null,
    conversationId: meta.conversationId || conversationIdFromTaskMeta(meta) || "",
    persist: meta.persist !== false && !isEphemeralTaskScope(meta.scope || "global"),
    backendSynced: false,
  };
  if (task.button) {
    task.button.disabled = true;
    task.button.dataset.taskLocked = task.id;
    task.button.dataset.taskScope = task.scope;
    createButtonTaskProgress(task);
  }
  state.tasks.unshift(task);
  pruneLocalTasks();
  recordTaskHistory(task, "created", "任务已加入队列");
  renderTaskQueue();
  syncBackendTask("create", task);
  syncTaskButtonLocks();
  return task;
}

function pruneLocalTasks() {
  const active = state.tasks.filter((task) => ["queued", "running"].includes(task.status));
  const terminal = state.tasks.filter((task) => !["queued", "running"].includes(task.status)).slice(0, 60);
  state.tasks = [...active, ...terminal].filter((task, index, items) =>
    items.findIndex((candidate) => candidate.id === task.id) === index
  );
}

async function executeTask(task, worker) {
  updateTask(task.id, {
    status: "running",
    startedAt: new Date().toISOString(),
    progress: Math.max(task.progress, 12),
    phase: "正在处理",
  });
  recordTaskHistory(task, "started", "任务开始执行");
  try {
    const helpers = {
      task,
      update: (progress, phase, detail = "") => updateTask(task.id, { progress, phase, detail }),
    };
    const result = await worker(helpers);
    if (["cancelled_by_user", "cancelled", "interrupted"].includes(String(result?.status || ""))) {
      finishTask(task, "cancelled", result?.message || "任务已取消", 100);
      return result;
    }
    if (taskResultFailed(result)) {
      finishTask(task, "failed", result?.message || result?.error || "操作失败", 100);
      return result;
    }
    finishTask(task, "completed", "处理完成", 100);
    return result;
  } catch (error) {
    finishTask(task, "failed", error.message || "操作失败", 100);
    setStatusMessage(`${task.label}失败：${error.message}`);
    return { status: "error", message: error.message };
  } finally {
    if (task.button && task.button.dataset.taskLocked === task.id) {
      delete task.button.dataset.taskLocked;
      task.button.disabled = false;
    }
    setDirtyIndicator(state.controlsSaving ? "saving" : (state.controlsDirty ? "dirty" : "clean"));
    renderAgentStatus();
    if (state.data?.weflow) renderWeFlow(state.data.weflow);
    syncTaskButtonLocks();
    syncDriverProbeAvailability();
  }
}

function finishTask(task, status, phase, progress) {
  updateTask(task.id, {
    status,
    phase,
    progress,
    finishedAt: new Date().toISOString(),
  });
  recordTaskHistory(task, status === "completed" ? "finished" : status, phase);
  pruneLocalTasks();
  syncTaskButtonLocks();
  setTimeout(() => removeButtonTaskProgress(task.id), 1600);
}

function updateTask(taskId, patch) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (patch.progress !== undefined) {
    patch.progress = clampPercent(patch.progress);
  }
  patch.updatedAt = patch.updatedAt || new Date().toISOString();
  Object.assign(task, patch);
  renderTaskQueue();
  updateButtonTaskProgress(task);
  syncBackendTask("update", task);
}

function syncBackendTask(action, task) {
  if (!task || task.backendOnly || task.persist === false) return;
  const taskId = task.id;
  const payload = {
    action,
    task_id: taskId,
    task: backendTaskPayload(task),
    patch: backendTaskPayload(task),
  };
  const previous = state.taskSyncChains.get(taskId) || Promise.resolve();
  const request = previous
    .catch(() => null)
    .then(async () => {
      try {
        await api("/api/tasks", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        task.backendSynced = true;
      } catch (error) {
        task.backendSynced = false;
      }
    });
  const cleanup = request.finally(() => {
    if (state.taskSyncChains.get(taskId) === cleanup) state.taskSyncChains.delete(taskId);
  });
  state.taskSyncChains.set(taskId, cleanup);
  return cleanup;
}

function backendTaskPayload(task) {
  return {
    task_id: task.id,
    title: task.label,
    kind: task.category,
    status: task.status,
    priority: Number.isFinite(Number(task.priority)) ? Number(task.priority) : 50,
    progress: clampPercent(task.progress),
    phase: task.phase || "",
    detail: task.detail || "",
    conversation_id: task.conversationId || conversationIdFromScope(task.scope),
    concurrency_key: task.scope || "global",
    started_at: task.startedAt || "",
    finished_at: task.finishedAt || "",
    updated_at: task.updatedAt || "",
    metadata: {
      scope_label: task.scopeLabel || taskScopeText(task.scope || "global"),
      target: task.target || "",
      local_ui: true,
      ephemeral: task.persist === false,
    },
  };
}

function isEphemeralTaskScope(scope = "") {
  return isNonLaneScope(scope);
}

function conversationIdFromTaskMeta(meta = {}) {
  return meta.conversationId || conversationIdFromScope(meta.scope || "");
}

function conversationIdFromScope(scope = "") {
  const text = String(scope || "");
  return text.startsWith("conversation:") ? text.slice("conversation:".length) : "";
}

function createButtonTaskProgress(task) {
  if (!task.button) return;
  const node = document.createElement("div");
  node.className = "button-progress-popover";
  node.innerHTML = `
    <div class="button-progress-head">
      <strong></strong>
      <span></span>
    </div>
    <div class="progress-track"><span></span></div>
    <div class="button-progress-phase"></div>
  `;
  document.body.append(node);
  state.taskPopovers.set(task.id, node);
  updateButtonTaskProgress(task);
}

function updateButtonTaskProgress(task) {
  const node = state.taskPopovers.get(task.id);
  if (!node || !task.button) return;
  const title = node.querySelector("strong");
  const status = node.querySelector(".button-progress-head span");
  const bar = node.querySelector(".progress-track span");
  const phase = node.querySelector(".button-progress-phase");
  title.textContent = task.label;
  status.textContent = `${taskAuditStatusText(task.status)} ${clampPercent(task.progress)}%`;
  bar.style.width = `${clampPercent(task.progress)}%`;
  phase.textContent = task.phase || task.detail || "";
  positionButtonTaskProgress(task.button, node);
}

function positionButtonTaskProgress(button, node) {
  if (!button.isConnected || button.offsetParent === null) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  const shellRect = $(".shell").getBoundingClientRect();
  const buttonRect = button.getBoundingClientRect();
  const nodeRect = node.getBoundingClientRect();
  const padding = 10;
  const gap = 8;
  const minLeft = shellRect.left + padding;
  const maxLeft = shellRect.right - nodeRect.width - padding;
  const idealLeft = buttonRect.left + buttonRect.width / 2 - nodeRect.width / 2;
  const left = Math.max(minLeft, Math.min(maxLeft, idealLeft));
  let top = buttonRect.bottom + gap;
  const tabs = $(".page-tabs");
  const tabsRect = tabs?.offsetParent === null ? null : tabs?.getBoundingClientRect();
  if (tabsRect && top < tabsRect.bottom + gap && top + nodeRect.height > tabsRect.top - gap) {
    top = tabsRect.bottom + gap;
  }
  if (top + nodeRect.height > window.innerHeight - padding) {
    top = Math.max(padding, buttonRect.top - nodeRect.height - gap);
  }
  const arrowX = buttonRect.left + buttonRect.width / 2 - left;
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
  node.style.setProperty("--tip-x", `${Math.round(Math.max(10, Math.min(nodeRect.width - 10, arrowX)))}px`);
}

function removeButtonTaskProgress(taskId) {
  const node = state.taskPopovers.get(taskId);
  if (!node) return;
  node.remove();
  state.taskPopovers.delete(taskId);
}

function repositionTaskProgressPopovers() {
  for (const task of state.tasks) {
    updateButtonTaskProgress(task);
  }
}

function recordTaskHistory(task, event, detail = "") {
  state.taskHistory.unshift({
    id: `${task.id}-${event}-${Date.now()}`,
    taskId: task.id,
    time: new Date().toISOString(),
    label: task.label,
    category: task.category,
    event,
    status: task.status,
    progress: clampPercent(task.progress),
    scopeLabel: task.scopeLabel,
    detail: detail || task.phase || "",
  });
  state.taskHistory = state.taskHistory.slice(0, 100);
  renderOperationHistory();
}

function renderTaskQueue() {
  renderOperationHistory();
  renderResourceCommandCenter();
  renderAgentStatus();
  renderResourceAudit();
  renderResourceScheduler();
  renderChannelTaskLanes();
}

function renderResourceCommandCenter() {
  const countNode = $("#taskActiveCount");
  const poolGrid = $("#resourcePoolGrid");
  const centralList = $("#centralTaskList");
  const centralCount = $("#centralTaskCount");
  if (!countNode || !poolGrid || !centralList || !centralCount) return;
  const manager = state.data?.task_manager || {};
  const counts = manager.counts || {};
  const tasks = combinedTasks();
  const activeCount = Math.max(Number(counts.active || 0), tasks.filter((task) => taskIsActive(task)).length);
  countNode.textContent = `${activeCount} 进行中`;
  renderResourcePools(poolGrid, manager.scheduler?.resource_pools || {});
  const centralTasks = orderedTasks(tasks.filter((task) => isCentralTask(task) && taskIsActive(task))).slice(0, 12);
  centralCount.textContent = `${centralTasks.length} 项`;
  centralList.innerHTML = "";
  if (!centralTasks.length) {
    centralList.append(emptyNode("暂无总台进程；会话任务会进入下方通道队列"));
    return;
  }
  for (const task of centralTasks) {
    const node = document.createElement("article");
    node.className = `central-task-item status-${task.status}`;
    node.innerHTML = `
      <div class="lane-task-head">
        <strong>${escapeHtml(task.label)}</strong>
        <span class="task-status">${escapeHtml(taskStatusText(task.status))}</span>
      </div>
      <div class="progress-track"><span style="width: ${clampPercent(task.progress)}%"></span></div>
      <span>${escapeHtml(task.category)} / ${escapeHtml(task.scopeLabel)}${task.backendOnly ? " / 后端" : ""} / ${escapeHtml(taskTimeRange(task))}</span>
      <span>${escapeHtml(task.phase || task.detail || "等待事件")}</span>
      <div class="task-inline-actions"></div>
      <div class="task-event-slot"></div>
    `;
    appendTaskEventControls(node, task);
    centralList.append(node);
  }
}

function renderAgentStatus() {
  const root = $("#agentStatusSummary");
  if (!root) return;
  const agent = state.data?.agent || {};
  const lastTick = agent.last_tick && typeof agent.last_tick === "object" ? agent.last_tick : {};
  const worker = agent.worker && typeof agent.worker === "object" ? agent.worker : {};
  const workerRunning = Boolean(worker.running);
  const startButton = $("#agentStartButton");
  const stopButton = $("#agentStopButton");
  if (startButton && !startButton.dataset.taskLocked) startButton.disabled = workerRunning || Boolean(worker.stop_requested);
  if (stopButton && !stopButton.dataset.taskLocked) stopButton.disabled = !workerRunning && !worker.stop_requested;
  root.innerHTML = "";
  root.append(agentStatusPill(`连续：${agentWorkerStatusText(worker)}`));
  if (workerRunning || worker.last_status) {
    root.append(agentStatusPill(`worker tick：${Number(worker.loops || 0)}`));
    root.append(agentStatusPill(`最近处理：${Number(worker.last_processed_count || 0)} 条`));
  }
  if (!lastTick.job_id) {
    root.append(agentStatusPill("状态：未运行"));
    root.append(agentStatusPill(`事件文件：${Number(agent.event_file_count || 0)}`));
    return;
  }
  const session = lastTick.session_summary && typeof lastTick.session_summary === "object" ? lastTick.session_summary : {};
  const cursor = lastTick.cursor && typeof lastTick.cursor === "object" ? lastTick.cursor : {};
  root.append(agentStatusPill(`状态：${taskStatusText(lastTick.status || agent.status || "idle")}`));
  root.append(agentStatusPill(`处理：${Number(lastTick.processed_count || 0)} 条`));
  root.append(agentStatusPill(`主动：${Number(lastTick.proactive_reply_count || 0)} 条`));
  root.append(agentStatusPill(`逐通道聚合：${Number(session.conversation_count || 0)} 通道`));
  const requestedTalkers = Array.isArray(lastTick.requested_talkers) ? lastTick.requested_talkers : [];
  const requestedConversations = Array.isArray(lastTick.requested_conversation_ids) ? lastTick.requested_conversation_ids : [];
  if (requestedTalkers.length || requestedConversations.length) {
    root.append(agentStatusPill(`作用域：${requestedTalkers.length || requestedConversations.length} 个选定通道`));
  }
  root.append(agentStatusPill(`待接：${Number(session.pending_user_count || 0)} 条`));
  const blockedPending = Number(session.blocked_pending_user_count || 0);
  const openingGreeting = Number(session.opening_greeting_count || 0);
  if (blockedPending > 0) root.append(agentStatusPill(`阻断：${blockedPending} 条等新消息`));
  if (openingGreeting > 0) root.append(agentStatusPill(`开场：${openingGreeting} 通道`));
  root.append(agentStatusPill(`游标：${Number(cursor.read_offset || 0)}`));
  if (worker.last_error) root.append(agentStatusPill(`worker 错误：${worker.last_error}`));
}

function agentStatusPill(text) {
  const node = document.createElement("span");
  node.textContent = text;
  node.title = text;
  return node;
}

function agentWorkerStatusText(worker) {
  if (!worker || typeof worker !== "object") return "未启动";
  if (worker.stop_requested) return "停止中";
  if (worker.running) {
    const status = String(worker.last_status || "").trim();
    if (status === "idle") return "运行中/空闲";
    if (status === "error") return "运行中/有错误";
    if (status === "starting") return "启动中";
    return "运行中";
  }
  if (worker.last_status === "stopped") return "已停止";
  if (worker.last_status === "error") return "已停止/有错误";
  return "未启动";
}

function renderResourcePools(root, pools) {
  root.innerHTML = "";
  const entries = Object.entries(pools || {});
  if (!entries.length) {
    root.append(emptyNode("暂无资源池状态"));
    return;
  }
  const order = { gpu: 0, llm_interactive: 1, llm_background: 2, llm: 3, media_cpu: 4, file_io: 5, wechat_io: 6, cpu_io: 7 };
  entries.sort(([left], [right]) => (order[left] ?? 9) - (order[right] ?? 9) || left.localeCompare(right));
  for (const [name, pool] of entries) {
    const maxParallel = Math.max(1, Number(pool?.max_parallel || 1));
    const active = Math.max(0, Number(pool?.active || 0));
    const queued = Math.max(0, Number(pool?.queued || 0));
    const percent = clampPercent((active / maxParallel) * 100);
    const node = document.createElement("article");
    node.className = "resource-card";
    node.innerHTML = `
      <div class="resource-card-head">
        <strong>${escapeHtml(resourcePoolText(name))}</strong>
        <span>${active}/${maxParallel}</span>
      </div>
      <div class="progress-track"><span style="width: ${percent}%"></span></div>
      <small>排队 ${queued} / 策略 ${escapeHtml(resourcePolicyText(name))}</small>
    `;
    root.append(node);
  }
}

function renderResourceAudit() {
  const root = $("#resourceAuditSummary");
  if (!root) return;
  const audit = state.data?.resource_audit || state.data?.task_manager?.scheduler?.resource_audit || {};
  const snapshot = audit.snapshot && typeof audit.snapshot === "object" ? audit.snapshot : {};
  const recommendation = audit.recommendation && typeof audit.recommendation === "object" ? audit.recommendation : {};
  root.innerHTML = "";
  if (!audit.status) {
    root.append(emptyNode("尚未执行本机资源审计；点击按钮后会缓存结果，并用于总台资源池并发建议。"));
    return;
  }
  const updated = audit.updated_at ? formatLocalTime(audit.updated_at) : "刚刚";
  const cards = [
    {
      label: "CPU",
      value: snapshot.cpu_name || "unknown",
      detail: `${snapshot.physical_cores || "-"}C/${snapshot.logical_processors || "-"}T · 当前 ${numberText(snapshot.cpu_percent, 1)}%`,
    },
    {
      label: "内存",
      value: `${snapshot.available_memory_mb || 0}/${snapshot.total_memory_mb || 0} MB`,
      detail: "可用 / 总量",
    },
    {
      label: "GPU",
      value: snapshot.gpu_name || "未发现 NVIDIA GPU",
      detail: snapshot.gpu_memory_total_mb
        ? `显存 ${snapshot.gpu_memory_used_mb || 0}/${snapshot.gpu_memory_total_mb || 0} MB`
        : "显式 GPU 档仍受 GPU gate 串行保护",
    },
    {
      label: "媒体并发",
      value: `CPU ${recommendation.media_cpu || "-"} · GPU ${recommendation.gpu_media || 1}`,
      detail: `OCR ${recommendation.ocr_cpu_parallel || "-"} / ASR ${recommendation.asr_cpu_parallel || "-"} / 文件 I/O ${recommendation.file_io_parallel || "-"}`,
    },
    {
      label: "模型划分",
      value: `交互 ${ratioText(recommendation.llm_interactive_ratio, 0.7)} / 后台 ${ratioText(recommendation.llm_background_ratio, 0.3)}`,
      detail: recommendation.thermal_risk ? `热风险 ${thermalRiskText(recommendation.thermal_risk)}` : "按密钥池总并发拆分",
    },
    {
      label: "审计时间",
      value: updated,
      detail: audit.storage ? "已缓存到 runtime/resource_audit.json" : "仅当前会话可见",
    },
  ];
  const grid = document.createElement("div");
  grid.className = "resource-audit-grid";
  for (const card of cards) {
    const node = document.createElement("article");
    node.className = "resource-audit-card";
    node.innerHTML = `
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.value)}</strong>
      <small>${escapeHtml(card.detail)}</small>
    `;
    grid.append(node);
  }
  root.append(grid);
  if (recommendation.reason) {
    const reason = document.createElement("p");
    reason.className = "resource-audit-reason";
    reason.textContent = recommendation.reason;
    root.append(reason);
  }
}

function renderResourceScheduler() {
  const root = $("#resourceSchedulerSummary");
  if (!root) return;
  const scheduler = state.data?.resource_scheduler || state.data?.task_manager?.scheduler?.resource_scheduler || {};
  root.innerHTML = "";
  if (!scheduler.schema) {
    root.append(emptyNode("暂无总台调度策略；完成资源审计或刷新后会显示前台/后台预算。"));
    return;
  }
  if (scheduler.status === "error") {
    root.append(emptyNode(`调度策略读取失败：${scheduler.error || "unknown"}`));
    return;
  }
  const interactive = scheduler.interactive && typeof scheduler.interactive === "object" ? scheduler.interactive : {};
  const background = scheduler.background && typeof scheduler.background === "object" ? scheduler.background : {};
  const policy = scheduler.policy && typeof scheduler.policy === "object" ? scheduler.policy : {};
  const auditTime = interactive.audit_updated_at || background.audit_updated_at || "";
  const cards = [
    {
      label: "前台交互",
      value: `${interactive.max_parallel_conversations || "-"} 路会话`,
      detail: `LLM ${interactive.llm_interactive || "-"} / 总 ${interactive.llm_total || "-"}，优先处理当前接话`,
    },
    {
      label: "后台任务",
      value: `${background.max_parallel_conversations || "-"} 路会话`,
      detail: `LLM ${background.llm_background || "-"} / 总 ${background.llm_total || "-"}，用于回填与 context-only`,
    },
    {
      label: "媒体解析",
      value: `CPU ${interactive.media_cpu || background.media_cpu || "-"} · GPU ${interactive.gpu_media || background.gpu_media || 1}`,
      detail: `文件 I/O ${interactive.file_io || background.file_io || "-"}；GPU heavy 仍由 GPU gate 排队`,
    },
    {
      label: "策略来源",
      value: auditTime ? formatLocalTime(auditTime) : "默认策略",
      detail: policy.source || "runtime/resource_audit.json + 当前密钥池并发",
    },
  ];
  const head = document.createElement("div");
  head.className = "resource-scheduler-head";
  head.innerHTML = `
    <strong>总台调度策略</strong>
    <span>模型调用按前台 70% / 后台 30% 拆分；历史回填不会挤占当前会话接话。</span>
  `;
  root.append(head);
  const grid = document.createElement("div");
  grid.className = "resource-scheduler-grid";
  for (const card of cards) {
    const node = document.createElement("article");
    node.className = "resource-scheduler-card";
    node.innerHTML = `
      <span>${escapeHtml(card.label)}</span>
      <strong>${escapeHtml(card.value)}</strong>
      <small>${escapeHtml(card.detail)}</small>
    `;
    grid.append(node);
  }
  root.append(grid);
  const preview = state.data?.task_manager?.scheduler?.dispatch_preview || {};
  if (preview && preview.schema) {
    root.append(renderDispatchPreview(preview));
  }
  const reasonText = interactive.reason || background.reason || policy.llm_split || "";
  if (reasonText) {
    const reason = document.createElement("p");
    reason.className = "resource-scheduler-reason";
    reason.textContent = reasonText;
    root.append(reason);
  }
}

function renderDispatchPreview(preview) {
  const root = document.createElement("section");
  root.className = "dispatch-preview-panel";
  const runnable = Array.isArray(preview.runnable) ? preview.runnable : [];
  const blocked = Array.isArray(preview.blocked) ? preview.blocked : [];
  root.innerHTML = `
    <div class="dispatch-preview-head">
      <strong>调度预览</strong>
      <span>可领取 ${escapeHtml(preview.runnable_count || runnable.length || 0)} / 阻塞 ${escapeHtml(preview.blocked_count || blocked.length || 0)}</span>
    </div>
  `;
  const list = document.createElement("div");
  list.className = "dispatch-preview-list";
  const visibleBlocked = blocked.filter((item) => item && item.reason).slice(0, 5);
  const visibleRunnable = runnable.slice(0, Math.max(0, 5 - visibleBlocked.length));
  for (const item of visibleBlocked) {
    list.append(renderDispatchPreviewItem(item, true));
  }
  for (const item of visibleRunnable) {
    list.append(renderDispatchPreviewItem(item, false));
  }
  if (!list.children.length) {
    list.append(emptyNode("暂无排队任务需要调度"));
  }
  root.append(list);
  return root;
}

function renderDispatchPreviewItem(item, blocked) {
  const node = document.createElement("div");
  node.className = blocked ? "dispatch-preview-item blocked" : "dispatch-preview-item runnable";
  const reason = blocked ? dispatchReasonText(item.reason || "") : "等待领取";
  node.innerHTML = `
    <strong>${escapeHtml(item.title || item.task_id || "任务")}</strong>
    <span>${escapeHtml(reason)}</span>
    <small>${escapeHtml(item.conversation_id || "global")} · ${escapeHtml(resourcePoolText(item.resource_class || "cpu_io"))}${configBoolean(item.channel_pinned, false) ? " · 置顶" : ""} · score ${escapeHtml(item.dispatch_score || 0)}</small>
  `;
  return node;
}

function renderChannelTaskLanes() {
  const list = $("#channelTaskList");
  const count = $("#channelLaneCount");
  if (!list || !count) {
    state.renderingChannelLanes = false;
    return;
  }
  // Replacing the active input during an IME composition discards the
  // unfinished text. Other panels may refresh, but this lane DOM stays put.
  if (channelLaneCompositionActive()) return;
  rememberRenderedChannelLaneOpenState(list);
  rememberRenderedChannelLaneDrafts(list);
  const focusedControl = channelLaneFocusedControl(list);
  const lanes = channelLaneViewModels();
  const items = Array.isArray(lanes) ? lanes : [];
  pruneMissingChannelLaneDrafts(items);
  const currentLaneIds = new Set(items.map((lane, index) => String(
    lane.conversation_id || lane.display_name || lane.current_topic?.topic_id || `lane-${index}`,
  )));
  count.textContent = items.length;
  state.renderingChannelLanes = true;
  list.innerHTML = "";
  if (!items.length) {
    list.append(emptyNode("暂无通道状态；完成一次拉取后会按会话生成独立 lane。"));
    state.renderingChannelLanes = false;
    return;
  }
  let laneIndex = 0;
  for (const lane of items.slice(0, 16)) {
    const fallbackLaneId = `lane-${laneIndex++}`;
    const laneId = String(lane.conversation_id || lane.display_name || lane.current_topic?.topic_id || fallbackLaneId);
    const tasks = orderedTasks(lane.tasks || []);
    const visibleTasks = laneVisibleTasks(tasks);
    const activeTasks = visibleTasks.filter((task) => taskIsActive(task));
    const historyTasks = visibleTasks.filter((task) => !taskIsActive(task)).slice(0, 3);
    const current = activeTasks[0] || visibleTasks[0] || {};
    const topic = lane.current_topic || {};
    const counts = taskCounts(visibleTasks);
    const audit = lane.resource_audit || {};
    const fileStates = Array.isArray(lane.file_states) ? lane.file_states : [];
    const replyState = lane.reply_state && typeof lane.reply_state === "object" ? lane.reply_state : {};
    const control = normalizeChannelControl(lane.control || {});
    const laneStatus = lane.effective_status || current.status || topic.status || "idle";
    const progress = laneStatus === "completed" ? 100 : clampPercent(current.progress || 0);
    const rememberedOpen = state.channelLaneOpenState.has(laneId) ? state.channelLaneOpenState.get(laneId) : null;
    const phase =
      current.phase ||
      current.detail ||
      channelControlPhase(control) ||
      (lane.topic_queue || lane.topic_history || []).slice(0, 3).map((item) => item.title || item.topic_id).filter(Boolean).join(" / ") ||
      channelIdlePhase(lane);
    const node = document.createElement("details");
    node.className = `channel-lane status-${laneStatus}`;
    node.dataset.laneId = laneId;
    node.open = rememberedOpen === null ? activeTasks.length > 0 : Boolean(rememberedOpen);
    node.innerHTML = `
      <summary>
        <div class="channel-lane-title">
          <strong>${escapeHtml(topic.title || lane.display_name || conversationLabel(lane.conversation_id) || lane.conversation_id || "未绑定通道")}</strong>
          <span class="task-status">${escapeHtml(taskStatusText(laneStatus))}</span>
        </div>
        <div class="channel-lane-meta">
          <span>${escapeHtml(conversationLabel(lane.conversation_id) || lane.conversation_id || "global")}</span>
          <span>任务 ${counts.active || 0}/${counts.total || 0} · 文件 ${fileStates.length} · 优先级 ${control.priority}</span>
        </div>
        <div class="progress-track"><span style="width: ${progress}%"></span></div>
        <div class="channel-lane-meta">
          <span>${progress}% · ${escapeHtml(channelControlModeText(control.mode))}</span>
          <span>资源 ${escapeHtml(String(audit.actual_cost || 0))}/${escapeHtml(String(audit.estimated_cost || 0))}</span>
        </div>
        <div class="task-phase">${escapeHtml(phase || "等待事件")}</div>
      </summary>
      <div class="lane-control-slot"></div>
      <div class="lane-task-list"></div>
      <div class="lane-state-grid"></div>
      <div class="lane-topic-list"></div>
    `;
    node.addEventListener("toggle", () => {
      if (state.renderingChannelLanes || !node.isConnected || node.dataset.userToggleArmed !== "1") return;
      state.channelLaneOpenState.set(laneId, node.open);
    });
    const controlSlot = node.querySelector(".lane-control-slot");
    controlSlot.append(renderLaneControl(lane, control));
    const taskList = node.querySelector(".lane-task-list");
    for (const task of [...activeTasks, ...historyTasks].slice(0, 10)) {
      taskList.append(renderLaneTask(task));
    }
    if (!taskList.children.length) {
      taskList.append(emptyNode("该通道暂无可展示任务"));
    }
    const stateGrid = node.querySelector(".lane-state-grid");
    const replyNode = renderLaneReplyState(replyState);
    if (replyNode) stateGrid.append(replyNode);
    const fileNode = renderLaneFileList(fileStates);
    if (fileNode) stateGrid.append(fileNode);
    const resourceNode = renderLaneResources(audit.resources || {});
    if (resourceNode) stateGrid.append(resourceNode);
    if (!stateGrid.children.length) {
      stateGrid.remove();
    }
    const topicList = node.querySelector(".lane-topic-list");
    for (const item of laneTopicItems(lane).slice(0, 6)) {
      const topicNode = document.createElement("span");
      const active = Number(item.active_count || 0);
      const taskCount = Number(item.task_count || 0);
      const terminal = Number(item.terminal_count || 0);
      if (taskCount > 0 && active === 0 && terminal >= taskCount) {
        topicNode.classList.add("is-terminal");
      }
      topicNode.textContent = `${item.title || item.topic_id || "主题"} · ${topicProgressText(item)}`;
      topicList.append(topicNode);
    }
    if (!topicList.children.length) {
      topicList.remove();
    }
    list.append(node);
    requestAnimationFrame(() => {
      if (node.isConnected) node.dataset.userToggleArmed = "1";
    });
  }
  for (const laneId of state.channelLaneOpenState.keys()) {
    if (!currentLaneIds.has(laneId) && !state.channelLaneDrafts.has(laneId)) {
      state.channelLaneOpenState.delete(laneId);
    }
  }
  state.renderingChannelLanes = false;
  restoreChannelLaneFocus(list, focusedControl);
}

function renderLaneTask(task) {
  const node = document.createElement("article");
  node.className = `lane-task status-${task.status}`;
  node.innerHTML = `
    <div class="lane-task-head">
      <strong>${escapeHtml(task.label)}</strong>
      <span class="task-status">${escapeHtml(taskStatusText(task.status))}</span>
    </div>
    <div class="progress-track"><span style="width: ${clampPercent(task.progress)}%"></span></div>
    <div class="lane-task-meta">
      <span>${clampPercent(task.progress)}% · ${escapeHtml(task.category)}</span>
      <span>${escapeHtml(taskTimeRange(task))}</span>
    </div>
    <div class="task-phase">${escapeHtml(task.phase || task.detail || "等待事件")}</div>
    <div class="task-inline-actions"></div>
    <div class="task-event-slot"></div>
  `;
  appendTaskEventControls(node, task);
  return node;
}

function rememberRenderedChannelLaneOpenState(list) {
  for (const node of list.querySelectorAll(".channel-lane[data-lane-id]")) {
    const laneId = String(node.dataset.laneId || "").trim();
    if (!laneId) continue;
    state.channelLaneOpenState.set(laneId, Boolean(node.open));
  }
}

const CHANNEL_LANE_DRAFT_FIELDS = {
  pinned: ".lane-pin-input",
  priority: ".lane-priority-input",
  snoozedUntil: ".lane-snooze-input",
  waitReason: ".lane-wait-input",
  operatorNote: ".lane-note-input",
};

function setChannelLaneComposition(conversationId, active) {
  const id = String(conversationId || "").trim();
  if (!id) return;
  if (active) {
    state.channelLaneComposing.add(id);
  } else {
    state.channelLaneComposing.delete(id);
  }
}

function channelLaneCompositionActive() {
  return state.channelLaneComposing.size > 0;
}

function pruneMissingChannelLaneDrafts(items) {
  const refreshRevision = Number(state.successfulRefreshRevision || 0);
  if (!refreshRevision || refreshRevision === state.channelLaneDraftPruneRevision) return;
  state.channelLaneDraftPruneRevision = refreshRevision;
  const present = new Set(
    (Array.isArray(items) ? items : [])
      .map((lane) => String(lane?.conversation_id || "").trim())
      .filter(Boolean),
  );
  for (const conversationId of present) {
    state.channelLaneMissingRefreshes.delete(conversationId);
  }
  for (const conversationId of Array.from(state.channelLaneDrafts.keys())) {
    if (present.has(conversationId)) continue;
    const missingCount = Number(state.channelLaneMissingRefreshes.get(conversationId) || 0) + 1;
    if (missingCount < 3) {
      state.channelLaneMissingRefreshes.set(conversationId, missingCount);
      continue;
    }
    state.channelLaneDrafts.delete(conversationId);
    state.channelLaneOpenState.delete(conversationId);
    state.channelLaneComposing.delete(conversationId);
    state.channelLaneMissingRefreshes.delete(conversationId);
  }
  for (const conversationId of Array.from(state.channelLaneMissingRefreshes.keys())) {
    if (!state.channelLaneDrafts.has(conversationId)) {
      state.channelLaneMissingRefreshes.delete(conversationId);
    }
  }
}

function channelLaneControlValues(root) {
  return {
    pinned: Boolean(root.querySelector(CHANNEL_LANE_DRAFT_FIELDS.pinned)?.checked),
    priority: String(root.querySelector(CHANNEL_LANE_DRAFT_FIELDS.priority)?.value || "50"),
    snoozedUntil: String(root.querySelector(CHANNEL_LANE_DRAFT_FIELDS.snoozedUntil)?.value || ""),
    waitReason: String(root.querySelector(CHANNEL_LANE_DRAFT_FIELDS.waitReason)?.value || ""),
    operatorNote: String(root.querySelector(CHANNEL_LANE_DRAFT_FIELDS.operatorNote)?.value || ""),
  };
}

function channelLaneControlDisplayValues(control = {}, draft = null) {
  const values = {
    pinned: control.pinned,
    priority: String(control.priority),
    snoozedUntil: datetimeLocalValue(control.snoozed_until),
    waitReason: control.wait_reason,
    operatorNote: control.operator_note,
  };
  const dirtyFields = new Set(draft?.dirtyFields || []);
  const draftValues = draft?.values || {};
  for (const field of Object.keys(values)) {
    if (dirtyFields.has(field) && Object.prototype.hasOwnProperty.call(draftValues, field)) {
      values[field] = draftValues[field];
    }
  }
  return values;
}

function rememberChannelLaneDraft(conversationId, root, { increment = true, field = "" } = {}) {
  const id = String(conversationId || "").trim();
  if (!id || !root) return null;
  const previous = state.channelLaneDrafts.get(id) || { revision: 0, dirtyFields: [], fieldRevisions: {} };
  const dirtyFields = new Set(previous.dirtyFields || []);
  if (field) dirtyFields.add(field);
  const revision = Number(previous.revision || 0) + (increment ? 1 : 0);
  const fieldRevisions = { ...(previous.fieldRevisions || {}) };
  if (field) fieldRevisions[field] = revision;
  const draft = {
    revision,
    values: channelLaneControlValues(root),
    dirtyFields: Array.from(dirtyFields),
    fieldRevisions,
  };
  state.channelLaneDrafts.set(id, draft);
  root.dataset.draftDirty = "1";
  return draft;
}

function rememberRenderedChannelLaneDrafts(list) {
  for (const lane of list.querySelectorAll(".channel-lane[data-lane-id]")) {
    const panel = lane.querySelector(".lane-control-panel[data-conversation-id]");
    if (!panel || panel.dataset.draftDirty !== "1") continue;
    rememberChannelLaneDraft(panel.dataset.conversationId, panel, { increment: false });
  }
}

function channelLaneFocusedControl(list) {
  const active = document.activeElement;
  if (!active || !list.contains(active)) return null;
  const lane = active.closest(".channel-lane[data-lane-id]");
  const field = String(active.dataset?.draftField || "");
  if (!lane || !field) return null;
  return {
    laneId: String(lane.dataset.laneId || ""),
    field,
    selectionStart: Number.isInteger(active.selectionStart) ? active.selectionStart : null,
    selectionEnd: Number.isInteger(active.selectionEnd) ? active.selectionEnd : null,
  };
}

function restoreChannelLaneFocus(list, snapshot) {
  if (!snapshot?.laneId || !snapshot.field) return;
  const lane = Array.from(list.querySelectorAll(".channel-lane[data-lane-id]"))
    .find((item) => item.dataset.laneId === snapshot.laneId);
  const input = lane?.querySelector(`[data-draft-field="${snapshot.field}"]`);
  if (!input) return;
  input.focus({ preventScroll: true });
  if (snapshot.selectionStart !== null && typeof input.setSelectionRange === "function") {
    input.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd ?? snapshot.selectionStart);
  }
}

function appendTaskEventControls(root, task) {
  const taskId = String(task.id || task.task_id || "").trim();
  const actions = root.querySelector(".task-inline-actions");
  const slot = root.querySelector(".task-event-slot");
  if (!actions || !slot || !taskId) {
    slot?.remove();
    actions?.remove();
    return;
  }
  const current = state.taskEvents.get(taskId) || {};
  const open = Boolean(current.open);
  actions.append(simpleButton(open ? "收起事件" : "事件", "ghost mini", () => toggleTaskEvents(taskId)));
  if (!open) {
    slot.remove();
    return;
  }
  slot.append(renderTaskEventList(taskId, current));
}

function renderTaskEventList(taskId, entry = {}) {
  const root = document.createElement("div");
  root.className = "task-event-list";
  if (entry.loading || state.taskEventsLoading.has(taskId)) {
    root.append(emptyNode("正在读取任务事件"));
    return root;
  }
  if (entry.error) {
    const node = document.createElement("div");
    node.className = "task-event-error";
    node.textContent = `事件读取失败：${entry.error}`;
    root.append(node);
    return root;
  }
  const events = Array.isArray(entry.events) ? entry.events : [];
  if (!events.length) {
    root.append(emptyNode("暂无任务事件"));
    return root;
  }
  for (const event of events.slice(0, 8)) {
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    const status = String(payload.status || "");
    const detail = [
      status ? taskStatusText(status) || status : "",
      payload.progress !== undefined ? `${clampPercent(payload.progress)}%` : "",
      payload.phase || payload.detail || payload.blocker || payload.last_error || "",
      payload.assigned_worker ? `worker ${payload.assigned_worker}` : "",
    ].filter(Boolean).join(" · ");
    const node = document.createElement("div");
    node.className = "task-event-item";
    node.innerHTML = `
      <strong>${escapeHtml(taskEventText(event.event || ""))}</strong>
      <span>${escapeHtml(formatLocalTime(event.created_at || payload.updated_at || payload.created_at || ""))}</span>
      <small>${escapeHtml(detail || compactText(payload.title || payload.task_id || "", 100) || "事件已记录")}</small>
    `;
    root.append(node);
  }
  return root;
}

async function toggleTaskEvents(taskId) {
  const id = String(taskId || "").trim();
  if (!id) return;
  const current = state.taskEvents.get(id) || {};
  if (current.open) {
    state.taskEvents.set(id, { ...current, open: false });
    renderTaskQueue();
    return;
  }
  if (current.loading || state.taskEventsLoading.has(id)) {
    state.taskEvents.set(id, { ...current, open: true });
    renderTaskQueue();
    return;
  }
  state.taskEvents.set(id, { ...current, open: true, loading: true, error: "" });
  state.taskEventsLoading.add(id);
  renderTaskQueue();
  try {
    const payload = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ action: "events", task_id: id, limit: 40 }),
    });
    const latest = state.taskEvents.get(id) || {};
    state.taskEvents.set(id, {
      ...latest,
      open: Boolean(latest.open),
      loading: false,
      error: "",
      events: Array.isArray(payload.events) ? payload.events : [],
    });
  } catch (error) {
    const latest = state.taskEvents.get(id) || {};
    state.taskEvents.set(id, {
      ...latest,
      open: Boolean(latest.open),
      loading: false,
      error: error.message || "unknown_error",
      events: [],
    });
  } finally {
    state.taskEventsLoading.delete(id);
    renderTaskQueue();
  }
}

function renderLaneControl(lane, control) {
  const conversationId = String(lane.conversation_id || "").trim();
  const draft = state.channelLaneDrafts.get(conversationId) || null;
  const values = channelLaneControlDisplayValues(control, draft);
  const node = document.createElement("section");
  node.className = `lane-control-panel mode-${control.mode}${control.pinned ? " is-pinned" : ""}`;
  node.dataset.conversationId = conversationId;
  if (draft) node.dataset.draftDirty = "1";
  node.innerHTML = `
    <div class="lane-state-head">
      <strong>通道控制</strong>
      <span>${escapeHtml(channelControlModeText(control.mode))} · 优先级 ${escapeHtml(control.priority)}${control.pinned ? " · 置顶" : ""}</span>
    </div>
    <div class="lane-control-actions"></div>
    <div class="lane-control-grid">
      <label class="lane-pin-row">
        <span>置顶</span>
        <input class="lane-pin-input" data-draft-field="pinned" type="checkbox" ${values.pinned ? "checked" : ""} />
      </label>
      <label>
        <span>优先级</span>
        <input class="compact-input lane-priority-input" data-draft-field="priority" type="number" min="0" max="100" step="1" value="${escapeHtml(values.priority)}" />
      </label>
      <label>
        <span>稍后到</span>
        <input class="compact-input lane-snooze-input" data-draft-field="snoozedUntil" type="datetime-local" value="${escapeHtml(values.snoozedUntil)}" />
      </label>
      <label>
        <span>等待原因</span>
        <input class="compact-input lane-wait-input" data-draft-field="waitReason" type="text" value="${escapeHtml(values.waitReason)}" />
      </label>
      <label>
        <span>备注</span>
        <input class="compact-input lane-note-input" data-draft-field="operatorNote" type="text" value="${escapeHtml(values.operatorNote)}" />
      </label>
    </div>
  `;
  node.querySelectorAll("[data-draft-field]").forEach((input) => {
    const eventName = input.type === "checkbox" ? "change" : "input";
    input.addEventListener(eventName, () => rememberChannelLaneDraft(conversationId, node, {
      field: String(input.dataset.draftField || ""),
    }));
    input.addEventListener("compositionstart", () => setChannelLaneComposition(conversationId, true));
    input.addEventListener("compositionend", () => {
      rememberChannelLaneDraft(conversationId, node, {
        field: String(input.dataset.draftField || ""),
      });
      setChannelLaneComposition(conversationId, false);
    });
  });
  const actions = node.querySelector(".lane-control-actions");
  const meta = {
    category: "通道",
    scope: `channels:control:${conversationId}`,
    scopeLabel: "通道控制事件",
    target: conversationId,
  };
  const actionClass = "ghost mini";
  const currentMode = control.mode;
  const canResume = currentMode !== "active";
  actions.append(actionButton("测文本", actionClass, (helpers) => queueChannelTestReply(conversationId, helpers), {
    category: "发送测试",
    scope: `conversation:${conversationId}`,
    scopeLabel: "通道发送探针",
    target: conversationId,
    label: `生成测试回复：${lane.display_name || conversationId}`,
  }));
  actions.append(actionButton("测文件", actionClass, (helpers) => queueChannelTestFile(conversationId, helpers), {
    category: "发送测试",
    scope: `conversation:${conversationId}`,
    scopeLabel: "通道文件探针",
    target: conversationId,
    label: `投递测试文件：${lane.display_name || conversationId}`,
  }));
  actions.append(actionButton(canResume ? "恢复" : "暂停", actionClass, () => {
    const waitReason = String(node.querySelector(".lane-wait-input")?.value || "人工暂停").trim() || "人工暂停";
    return updateChannelControl(conversationId, {
      action: canResume ? "resume" : "pause",
      wait_reason: waitReason,
    });
  }, { ...meta, label: `${canResume ? "恢复" : "暂停"}通道：${lane.display_name || conversationId}` }));
  actions.append(actionButton("静音", actionClass, () => updateChannelControl(conversationId, { action: "mute" }), {
    ...meta,
    label: `静音通道：${lane.display_name || conversationId}`,
  }));
  actions.append(actionButton(control.pinned ? "取消置顶" : "置顶", actionClass, () => updateChannelControl(conversationId, {
    action: control.pinned ? "unpin" : "pin",
  }), {
    ...meta,
    label: `${control.pinned ? "取消置顶" : "置顶"}通道：${lane.display_name || conversationId}`,
  }));
  actions.append(actionButton("稍后", actionClass, () => {
    const raw = String(node.querySelector(".lane-snooze-input")?.value || "").trim() || datetimeLocalInMinutes(30);
    return updateChannelControl(conversationId, {
      action: "snooze",
      snoozed_until: datetimeLocalToIso(raw),
    });
  }, { ...meta, label: `稍后处理通道：${lane.display_name || conversationId}` }));
  actions.append(actionButton(
    "保存",
    "primary mini",
    () => saveChannelLaneControl(conversationId, node),
    { ...meta, label: `保存通道控制：${lane.display_name || conversationId}` },
  ));
  return node;
}

function channelLaneViewModels() {
  const visibleChannels = Array.isArray(state.data?.channels?.items) ? state.data.channels.items : [];
  const channelStates = Array.isArray(state.data?.channel_states) ? state.data.channel_states : [];
  const backendLanes = Array.isArray(state.data?.task_manager?.channels) ? state.data.task_manager.channels : [];
  const laneById = new Map();
  for (const channel of visibleChannels) {
    const lane = normalizeChannelStateLane(channel?.state || {}, channel);
    const id = String(lane?.conversation_id || "").trim();
    if (!id) continue;
    laneById.set(id, lane);
  }
  if (!laneById.size) {
    for (const record of channelStates) {
      const lane = normalizeChannelStateLane(record || {}, null);
      const id = String(lane?.conversation_id || "").trim();
      if (!id) continue;
      laneById.set(id, lane);
    }
  }
  for (const record of backendLanes) {
    const lane = normalizeChannelStateLane(record || {}, null);
    const id = String(lane?.conversation_id || "").trim();
    if (!id) continue;
    laneById.set(id, mergeLaneState(laneById.get(id), lane));
  }
  for (const task of combinedTasks()) {
    if (!task.conversationId || isCentralTask(task)) continue;
    const id = task.conversationId;
    const lane = laneById.get(id) || {
      conversation_id: id,
      display_name: conversationLabel(id) || id,
      current_topic: {},
      topic_queue: [],
      topic_history: [],
      file_states: [],
      reply_state: {},
      resource_audit: {},
      control: normalizeChannelControl({}),
      effective_status: "idle",
      tasks: [],
    };
    lane.tasks = dedupeLaneTasks([...(lane.tasks || []), task]);
    laneById.set(id, lane);
  }
  return Array.from(laneById.values())
    .map((lane) => ({ ...lane, counts: taskCounts(laneVisibleTasks(lane.tasks || [])) }))
    .sort((left, right) => laneSortScore(right) - laneSortScore(left));
}

function normalizeChannelStateLane(record, channel = null) {
  const payload = record && typeof record === "object" ? record : {};
  const conversationId = String(payload.conversation_id || channel?.conversation_id || "").trim();
  if (!conversationId) return null;
  const activeTasks = Array.isArray(payload.active_tasks) ? payload.active_tasks : [];
  const taskHistory = Array.isArray(payload.task_history) ? payload.task_history : [];
  const active = Array.isArray(payload.active) ? payload.active : [];
  const history = Array.isArray(payload.history) ? payload.history : [];
  const tasks = dedupeLaneTasks([...activeTasks, ...taskHistory, ...active, ...history].map(normalizeBackendTask));
  const channelName = channel ? channelDisplayName(channel) : "";
  return {
    conversation_id: conversationId,
    conversation_type: String(payload.conversation_type || channel?.conversation_type || ""),
    display_name: channelName || String(payload.chat_title || channel?.chat_title || conversationLabel(conversationId) || conversationId),
    current_topic: payload.current_topic && typeof payload.current_topic === "object" ? payload.current_topic : {},
    topic_queue: Array.isArray(payload.topic_queue) ? payload.topic_queue : [],
    topic_history: Array.isArray(payload.topic_history) ? payload.topic_history : [],
    file_states: Array.isArray(payload.file_states) ? payload.file_states : [],
    reply_state: payload.reply_state && typeof payload.reply_state === "object" ? payload.reply_state : {},
    resource_audit: payload.resource_audit && typeof payload.resource_audit === "object" ? payload.resource_audit : {},
    control: normalizeChannelControl(payload.control || {}),
    effective_status: String(payload.effective_status || ""),
    last_user_message_at: String(payload.last_user_message_at || ""),
    last_agent_reply_at: String(payload.last_agent_reply_at || ""),
    last_message_at: String(payload.last_message_at || payload.updated_at || channel?.updated_at || ""),
    message_count: Number(payload.message_count || 0),
    updated_at: String(payload.updated_at || channel?.updated_at || ""),
    tasks,
  };
}

function mergeLaneState(existing, incoming) {
  if (!existing) return incoming;
  return {
    ...incoming,
    ...existing,
    current_topic: Object.keys(existing.current_topic || {}).length ? existing.current_topic : incoming.current_topic,
    topic_queue: (existing.topic_queue || []).length ? existing.topic_queue : incoming.topic_queue,
    topic_history: (existing.topic_history || []).length ? existing.topic_history : incoming.topic_history,
    file_states: (existing.file_states || []).length ? existing.file_states : incoming.file_states,
    reply_state: Object.keys(existing.reply_state || {}).length ? existing.reply_state : incoming.reply_state,
    resource_audit: Object.keys(existing.resource_audit || {}).length ? existing.resource_audit : incoming.resource_audit,
    control: Object.keys(existing.control || {}).length ? existing.control : incoming.control,
    effective_status: existing.effective_status || incoming.effective_status,
    tasks: dedupeLaneTasks([...(existing.tasks || []), ...(incoming.tasks || [])]),
  };
}

function dedupeLaneTasks(tasks) {
  const byId = new Map();
  for (const task of tasks || []) {
    if (!task || typeof task !== "object") continue;
    const id = String(task.id || task.task_id || "");
    const key = id || `${task.label || task.title || ""}:${task.status || ""}:${task.queuedAt || task.updated_at || ""}`;
    if (!key) continue;
    byId.set(key, task.id ? task : normalizeBackendTask(task));
  }
  return Array.from(byId.values());
}

function normalizeChannelControl(value = {}) {
  const payload = value && typeof value === "object" ? value : {};
  const mode = ["active", "paused", "muted", "snoozed"].includes(String(payload.mode || "").toLowerCase())
    ? String(payload.mode || "").toLowerCase()
    : "active";
  const priority = Number(payload.priority ?? 50);
  return {
    mode,
    pinned: configBoolean(payload.pinned, false),
    priority: Number.isFinite(priority) ? Math.max(0, Math.min(100, priority)) : 50,
    wait_reason: String(payload.wait_reason || ""),
    operator_note: String(payload.operator_note || ""),
    snoozed_until: String(payload.snoozed_until || ""),
    updated_at: String(payload.updated_at || ""),
    updated_by: String(payload.updated_by || ""),
  };
}

function channelControlModeText(mode) {
  return {
    active: "自动调度",
    paused: "人工暂停",
    muted: "静音",
    snoozed: "稍后处理",
  }[String(mode || "active")] || "自动调度";
}

function channelControlPhase(control) {
  if (!control || control.mode === "active") return "";
  if (control.mode === "paused") return control.wait_reason || control.operator_note || "人工暂停中";
  if (control.mode === "snoozed") return control.snoozed_until ? `稍后处理至 ${shortTime(control.snoozed_until)}` : "稍后处理";
  if (control.mode === "muted") return control.operator_note || "通道已静音";
  return "";
}

function datetimeLocalValue(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return datetimeLocalFromDate(date);
}

function datetimeLocalInMinutes(minutes) {
  return datetimeLocalFromDate(new Date(Date.now() + Math.max(1, Number(minutes) || 30) * 60 * 1000));
}

function datetimeLocalFromDate(date) {
  const offsetMs = date.getTimezoneOffset() * 60 * 1000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

function datetimeLocalToIso(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toISOString();
}

function laneTopicItems(lane) {
  const seen = new Set();
  const items = [];
  for (const item of [...(lane.topic_queue || []), ...(lane.topic_history || [])]) {
    const id = String(item?.topic_id || item?.title || "").trim();
    if (!id || seen.has(id)) continue;
    if (!shouldShowLaneTopicItem(item)) continue;
    seen.add(id);
    items.push(item);
  }
  return items;
}

function shouldShowLaneTopicItem(item) {
  const active = Number(item?.active_count || 0);
  if (active > 0) return true;
  const total = Number(item?.task_count || 0);
  const terminal = Number(item?.terminal_count || 0);
  if (!(total > 0 && terminal >= total)) return true;
  const stamp = Date.parse(String(item?.updated_at || ""));
  if (!Number.isFinite(stamp)) return false;
  const ageMs = Date.now() - stamp;
  return Number.isFinite(ageMs) && ageMs >= 0 && ageMs < 30 * 60 * 1000;
}

function topicProgressText(item) {
  const active = Number(item?.active_count || 0);
  const total = Number(item?.task_count || 0);
  const terminal = Number(item?.terminal_count || 0);
  const topicId = String(item?.topic_id || "");
  if (total > 0 && active === 0 && terminal >= total) {
    return topicId.startsWith("file:") ? `文件已完成 ${terminal}/${total}` : `已完成 ${terminal}/${total}`;
  }
  if (active > 0) return `进行中 ${active}/${total || active}`;
  if (total > 0) return `等待 ${active}/${total}`;
  return "已记录";
}

function channelIdlePhase(lane) {
  const reply = lane.reply_state && typeof lane.reply_state === "object" ? lane.reply_state : {};
  if (reply.status && reply.status !== "idle" && !configBoolean(reply.historical, false)) {
    const reason = String(reply.last_send_reason || "");
    const statusText = replyDisplayStatusText(reply, reply.status, reason);
    return `最近回复：${statusText} ${shortTime(reply.last_reply_at || "")}`;
  }
  if (lane.last_message_at) {
    return `最近消息 ${shortTime(lane.last_message_at)} · ${Number(lane.message_count || 0)} 条已入账`;
  }
  if ((lane.file_states || []).length) {
    return "文件状态已同步";
  }
  return "通道空闲";
}

function renderLaneReplyState(reply) {
  if (!reply || typeof reply !== "object") return null;
  const status = String(reply.status || reply.last_send_status || "idle");
  if (status === "idle" && !reply.last_reply_at && !reply.last_reply_entry_id) return null;
  const reason = String(reply.last_send_reason || "");
  const statusText = replyDisplayStatusText(reply, status, reason);
  const node = document.createElement("article");
  node.className = "lane-reply-state";
  node.innerHTML = `
    <div class="lane-state-head">
      <strong>回复状态</strong>
      <span>${escapeHtml(statusText)}</span>
    </div>
    <small>${escapeHtml(shortTime(reply.last_reply_at || "")) || "尚无回复"} · ${escapeHtml(reason || reply.last_send_status || status)}</small>
    <small>${escapeHtml(compactText(reply.last_reply_message_id || reply.last_reply_entry_id || "", 80))}</small>
  `;
  return node;
}

function replyDisplayStatusText(reply, status, reason = "") {
  if (configBoolean(reply?.historical, false) && status === "failed") return "历史发送失败";
  if (status === "sent" && String(reason || "").includes("dry_run_not_delivered")) return "演练完成（未投递）";
  return replyStatusText(status);
}

function renderLaneFileList(files) {
  if (!Array.isArray(files) || !files.length) return null;
  const node = document.createElement("article");
  node.className = "lane-file-list";
  const items = files.slice(0, 4).map((file) => {
    const points = Array.isArray(file.key_points) ? file.key_points.filter(Boolean).slice(0, 2) : [];
    const brief = String(file.summary || points.join("；") || "").trim();
    return `
      <div class="lane-file-item">
        <strong>${escapeHtml(file.name || file.file_id || "文件")}</strong>
        <span>解析 ${escapeHtml(file.parse_status || file.status || "unknown")} · AI ${escapeHtml(file.ai_analysis_status || "unknown")} · chunks ${escapeHtml(file.chunk_count || 0)}</span>
        ${brief ? `<small>${escapeHtml(compactText(brief, 120))}</small>` : ""}
      </div>
    `;
  }).join("");
  node.innerHTML = `
    <div class="lane-state-head">
      <strong>文件状态</strong>
      <span>${files.length} 个</span>
    </div>
    ${items}
    ${files.length > 4 ? `<small>另有 ${files.length - 4} 个文件已折叠</small>` : ""}
  `;
  return node;
}

function renderLaneResources(resources) {
  const entries = Object.entries(resources || {}).filter(([, value]) => value && typeof value === "object");
  if (!entries.length) return null;
  const node = document.createElement("article");
  node.className = "lane-resource-state";
  node.innerHTML = `
    <div class="lane-state-head">
      <strong>资源占用</strong>
      <span>${entries.length} 池</span>
    </div>
    ${entries.slice(0, 5).map(([name, pool]) => `
      <small>${escapeHtml(resourcePoolText(name))} ${escapeHtml(pool.active || 0)}/${escapeHtml(pool.max_parallel || 1)} · 排队 ${escapeHtml(pool.queued || 0)}</small>
    `).join("")}
  `;
  return node;
}

function renderOperationHistory() {
  const list = $("#operationHistoryList");
  const count = $("#operationHistoryCount");
  if (!list || !count) return;
  count.textContent = state.taskHistory.length;
  list.innerHTML = "";
  if (!state.taskHistory.length) {
    list.append(emptyNode("暂无操作历史"));
    return;
  }
  for (const entry of state.taskHistory.slice(0, 50)) {
    const node = document.createElement("div");
    node.className = "history-entry";
    const auditStatus = taskAuditStatusText(entry.status);
    node.innerHTML = `
      <div class="history-entry-time">${escapeHtml(formatLocalTime(entry.time))}</div>
      <div class="history-entry-action">${escapeHtml(entry.category)} / ${escapeHtml(taskEventText(entry.event))} / ${escapeHtml(entry.label)}</div>
      <div class="progress-track audit-progress" title="${escapeHtml(auditStatus)}">
        <span style="width: ${clampPercent(entry.progress)}%"></span>
        <em>${escapeHtml(auditStatus)}</em>
      </div>
      <div class="history-entry-result">${escapeHtml(entry.detail || entry.scopeLabel || "")}</div>
    `;
    list.append(node);
  }
}

function taskIsActive(task) {
  return ["queued", "running", "waiting", "paused", "blocked"].includes(String(task?.status || ""));
}

function laneVisibleTasks(tasks = []) {
  return (tasks || []).filter((task) => taskIsActive(task) || shouldShowLaneHistoryTask(task));
}

function shouldShowLaneHistoryTask(task) {
  const status = String(task?.status || "");
  if (!["completed", "failed", "cancelled"].includes(status)) return true;
  const stamp = taskRecencyStamp(task);
  if (!stamp) return false;
  const ageMs = Date.now() - stamp;
  return Number.isFinite(ageMs) && ageMs >= 0 && ageMs < 30 * 60 * 1000;
}

function isCentralTask(task) {
  const scope = String(task?.scope || "");
  return !task?.conversationId || isNonLaneScope(scope);
}

function isNonLaneScope(scope = "") {
  const value = String(scope || "");
  return (
    value.startsWith("diagnostic:") ||
    value.startsWith("agent:") ||
    value.startsWith("ui:") ||
    value.startsWith("weflow:") ||
    value.startsWith("queue:") ||
    value.startsWith("send-review:") ||
    value.startsWith("settings:") ||
    value.startsWith("audit:") ||
    value.startsWith("history:") ||
    value.startsWith("channels:")
  );
}

function taskCounts(tasks = []) {
  const counts = { active: 0, total: tasks.length };
  for (const task of tasks) {
    const status = String(task.status || "queued");
    counts[status] = (counts[status] || 0) + 1;
    if (taskIsActive(task)) counts.active += 1;
  }
  return counts;
}

function laneSortScore(lane) {
  const tasks = laneVisibleTasks(lane.tasks || []);
  const active = tasks.filter((task) => taskIsActive(task));
  const current = orderedTasks(active.length ? active : tasks)[0] || {};
  const control = normalizeChannelControl(lane.control || {});
  const activeBonus = taskIsActive(current) ? 1000 : 0;
  const fileBonus = Math.min(50, (lane.file_states || []).length * 5);
  const recency = String(lane.updated_at || lane.last_message_at || "");
  const controlBonus = (control.priority - 50) * 6;
  const pinnedBonus = control.pinned ? 1200 : 0;
  return pinnedBonus + Number(current.priority || 0) * 10 + controlBonus + activeBonus + fileBonus + clampPercent(current.progress || 0) + (recency ? 1 : 0);
}

function orderedTasks(tasks = state.tasks) {
  const priority = { running: 0, queued: 1, waiting: 2, blocked: 3, paused: 4, failed: 5, cancelled: 6, completed: 7 };
  return [...tasks].sort((left, right) => {
    const byStatus = (priority[left.status] ?? 9) - (priority[right.status] ?? 9);
    if (byStatus) return byStatus;
    const byPriority = Number(right.priority || 0) - Number(left.priority || 0);
    if (byPriority) return byPriority;
    return taskRecencyStamp(right) - taskRecencyStamp(left);
  });
}

function taskRecencyStamp(task) {
  for (const value of [task?.updatedAt, task?.finishedAt, task?.startedAt, task?.queuedAt]) {
    const stamp = Date.parse(String(value || ""));
    if (Number.isFinite(stamp)) return stamp;
  }
  return 0;
}

function resourcePoolText(name) {
  return {
    gpu: "GPU",
    llm: "模型调用",
    llm_interactive: "交互模型",
    llm_background: "后台模型",
    wechat_io: "微信 I/O",
    cpu_io: "CPU/文件 I/O",
    media_cpu: "媒体 CPU",
    file_io: "文件 I/O",
  }[name] || name || "资源";
}

function resourcePolicyText(name) {
  return {
    gpu: "仅显式 GPU 档进入，默认 1 路排队",
    llm: "按每 key 并发与密钥池汇总",
    llm_interactive: "保留约 70% 给用户当前交互",
    llm_background: "后台分析最多约 30%，避免挤占接话",
    wechat_io: "单通道写入，保护游标",
    cpu_io: "可并发，受磁盘影响",
    media_cpu: "轻型 OCR/ASR 主路径，按本机审计调整",
    file_io: "文件读取与索引并发，按磁盘压力调整",
  }[name] || "按任务管理器配置";
}

function dispatchReasonText(reason) {
  const text = String(reason || "");
  if (!text) return "可领取";
  if (text.startsWith("channel_paused:")) {
    const conversationId = text.slice("channel_paused:".length);
    return `通道暂停：${conversationLabel(conversationId) || conversationId}`;
  }
  if (text.startsWith("channel_snoozed:")) {
    const rest = text.slice("channel_snoozed:".length);
    const [conversationId, ...untilParts] = rest.split(":");
    const until = untilParts.join(":");
    return `稍后处理：${conversationLabel(conversationId) || conversationId}${until ? ` 至 ${shortTime(until)}` : ""}`;
  }
  if (text.startsWith("waiting_for_dependencies:")) return `等待依赖：${text.slice("waiting_for_dependencies:".length) || "未完成"}`;
  if (text.startsWith("resource_busy:")) return `资源忙：${resourcePoolText(text.slice("resource_busy:".length))}`;
  if (text.startsWith("channel_busy:")) {
    const conversationId = text.slice("channel_busy:".length);
    return `通道已有任务运行：${conversationLabel(conversationId) || conversationId}`;
  }
  if (text.startsWith("concurrency_key_busy:")) return `同作用域串行中：${text.slice("concurrency_key_busy:".length)}`;
  return text;
}

function combinedTasks() {
  const local = state.tasks.map((task) => ({ ...task, backendOnly: false }));
  const localIds = new Set(local.map((task) => task.id));
  const backend = backendTaskManagerTasks()
    .map(normalizeBackendTask)
    .filter((task) => task.id && !localIds.has(task.id));
  return [...local, ...backend];
}

function backendTaskManagerTasks() {
  const manager = state.data?.task_manager || {};
  const tasks = manager.tasks || [];
  return Array.isArray(tasks) ? tasks : [];
}

function normalizeBackendTask(task) {
  const id = String(task.task_id || task.id || "");
  const scope = String(task.concurrency_key || task.scope || "global");
  const metadata = task.metadata && typeof task.metadata === "object" ? task.metadata : {};
  return {
    id,
    label: String(task.title || task.label || id || "后台任务"),
    category: String(task.kind || task.category || "operation"),
    scope,
    scopeLabel: String(metadata.scope_label || task.scope_label || taskScopeText(scope)),
    target: String(metadata.target || task.target || ""),
    status: String(task.status || "queued"),
    progress: clampPercent(task.progress || 0),
    phase: String(task.phase || ""),
    detail: String(task.detail || task.blocker || task.last_error || ""),
    priority: Number(task.priority || 0),
    queuedAt: String(task.created_at || task.queuedAt || ""),
    startedAt: String(task.started_at || task.startedAt || ""),
    finishedAt: String(task.finished_at || task.finishedAt || ""),
    updatedAt: String(task.updated_at || task.updatedAt || task.finished_at || task.started_at || task.created_at || ""),
    conversationId: String(task.conversation_id || task.conversationId || ""),
    assignedWorker: String(task.assigned_worker || task.assignedWorker || ""),
    backendOnly: true,
  };
}

function taskResultFailed(result) {
  if (!result || typeof result !== "object") return false;
  return ["error", "failed", "partial_error", "blocked", "conflict"].includes(String(result.status || ""));
}

function taskStatusText(status) {
  return {
    idle: "空闲",
    queued: "排队中",
    running: "处理中",
    waiting: "等待输入",
    paused: "已暂停",
    muted: "静音",
    snoozed: "稍后",
    blocked: "已阻塞",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status || "";
}

function replyStatusText(status) {
  return {
    idle: "空闲",
    drafted: "已起草",
    dry_run: "演练",
    queued_for_confirm: "待审核",
    queued_to_bridge: "已入非前台桥",
    accepted: "端口已接收（未验证）",
    sent: "已发送",
    failed: "发送失败",
    skipped: "已跳过",
  }[status] || taskStatusText(status) || status || "";
}

function taskAuditStatusText(status) {
  return {
    queued: "待审查",
    running: "审查中",
    waiting: "等待补充",
    paused: "审查暂停",
    blocked: "审查阻塞",
    completed: "审查完成",
    failed: "审查失败",
    cancelled: "审查取消",
  }[status] || taskStatusText(status) || "未知状态";
}

function taskEventText(event) {
  const value = String(event || "");
  if (value.startsWith("transition:")) {
    const status = value.slice("transition:".length);
    return `转为${taskStatusText(status) || status}`;
  }
  return {
    created: "创建",
    updated: "更新",
    claimed: "领取",
    started: "开始",
    finished: "结束",
    finish_external: "外部完成",
    failed: "失败",
    cancelled: "取消",
  }[value] || value || "";
}

function taskScopeText(scope) {
  const value = String(scope || "");
  if (value.startsWith("conversation:")) return "同会话串行";
  if (value.startsWith("agent:")) return "对话 Agent";
  if (value.startsWith("weflow:exclusive")) return "WeFlow 独占队列";
  if (value.startsWith("weflow:pull")) return "WeFlow 拉取串行";
  if (value.startsWith("weflow:")) return "WeFlow 独立队列";
  if (value.startsWith("queue:") || value.startsWith("send-review:")) return "审核队列事件";
  if (value.startsWith("settings:")) return "设置串行";
  if (value.startsWith("diagnostic:")) return "诊断队列";
  if (value.startsWith("ui:")) return "界面队列";
  return "全局队列";
}

function taskTimeRange(task) {
  const start = task.startedAt || task.queuedAt;
  const end = task.finishedAt ? `-${formatLocalTime(task.finishedAt)}` : "";
  return `${formatLocalTime(start)}${end}`;
}

function formatLocalTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function numberText(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toFixed(digits);
}

function ratioText(value, fallback) {
  const number = Number.isFinite(Number(value)) ? Number(value) : Number(fallback);
  return `${Math.round(Math.max(0, number) * 100)}%`;
}

function thermalRiskText(value) {
  return {
    low: "低",
    medium: "中",
    high: "高",
  }[String(value || "").toLowerCase()] || String(value || "未知");
}

function bytesToMegabytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return 20;
  return Math.max(1, Math.round(number / 1024 / 1024));
}

function megabytesToBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return 20 * 1024 * 1024;
  return Math.max(1024, Math.round(number * 1024 * 1024));
}

function clampPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, Math.round(number)));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function saveControls() {
  if (state.controlsSaving) return { status: "blocked", message: "controls_save_in_progress" };
  state.controlsSaving = true;
  invalidateDriverProbeOverlay();
  syncDriverProbeAvailability();
  const savedRevision = state.controlsRevision;
  setDirtyIndicator("saving");
  renderSendControlSummary();
  try {
    const mode = currentMode("");
    if (!mode) {
      throw new Error("请选择发送模式");
    }
    const driver = String($("#driverSelect")?.value || "").trim();
    if (!driver) {
      throw new Error("请选择发送驱动");
    }
    await api("/api/controls", {
      method: "POST",
      body: JSON.stringify({
        mode,
        send_enabled: Boolean($("#sendEnabled")?.checked),
        send_driver: driver,
        send_backend: selectedSendBackend($("#sendBackendSelect")?.value),
        send_confirm_required: mode !== "auto",
        ocr_mode: runtimeMode($("#ocrModeSelect")?.value),
        asr_mode: runtimeMode($("#asrModeSelect")?.value),
        file_max_bytes: megabytesToBytes($("#fileMaxMb")?.value || 20),
      }),
    });
    const unchanged = state.controlsRevision === savedRevision;
    state.controlsDirty = !unchanged;
    setStatusMessage(unchanged ? "发送控制已保存" : "发送控制已保存；还有新的修改待保存");
    await refresh({ forceControls: unchanged, force: true });
    return { status: "ok" };
  } finally {
    state.controlsSaving = false;
    setDirtyIndicator(state.controlsDirty ? "dirty" : "clean");
    renderSendControlSummary();
    syncDriverProbeAvailability();
  }
}

async function queueAction(queueId, action) {
  const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({ reviewer: "sidebar" }),
  });
  const nextStatus = payload.queue_status || payload.item?.status || payload.status;
  if (nextStatus && state.data?.queues?.[nextStatus]) {
    setActiveStatus(nextStatus);
  }
  if (payload.status === "blocked") {
    setStatusMessage(`${actionText(action)}被阻塞：${payload.reason || "等待配置恢复"}`);
  } else {
    setStatusMessage(`${actionText(action)}完成`);
  }
  await refresh({ force: true });
  return payload;
}

async function removeQueueItem(queueId) {
  if (!queueId) return;
  if (!window.confirm("确定移除这个审核队列项吗？")) {
    return { status: "cancelled_by_user", message: "用户取消移除队列项" };
  }
  const payload = await api(`/api/queue/${encodeURIComponent(queueId)}/remove`, {
    method: "POST",
    body: JSON.stringify({ reviewer: "sidebar", note: "sidebar_remove_queue_item" }),
  });
  setStatusMessage("队列项已移除");
  await refresh({ force: true });
  return payload;
}

async function deleteChannel(conversationId) {
  if (!conversationId) return;
  const label = conversationLabel(conversationId) || conversationId;
  if (!window.confirm(`确定清除通道“${label}”吗？`)) {
    return { status: "cancelled_by_user", message: "用户取消清除通道" };
  }
  const payload = await api(`/api/channels/delete/${encodeURIComponent(conversationId)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatusMessage(payload.note || "通道已清除");
  await refresh({ force: true });
  return payload;
}

async function updateChannelControl(conversationId, payload = {}) {
  if (!conversationId) return null;
  const actionFields = channelControlActionDraftFields(payload.action);
  const expectedFieldRevisions = Object.fromEntries(actionFields.map((field) => [
    field,
    Number(state.channelLaneDrafts.get(conversationId)?.fieldRevisions?.[field] ?? -1),
  ]));
  const response = await api("/api/channel-state", {
    method: "POST",
    body: JSON.stringify({
      ...payload,
      conversation_id: conversationId,
      updated_by: "sidebar",
    }),
  });
  const control = response.channel_state?.control || {};
  reconcileChannelLaneActionDrafts(conversationId, actionFields, expectedFieldRevisions, control);
  setStatusMessage(`通道控制已更新：${channelControlModeText(control.mode || "active")}`);
  await refresh({ force: true });
  return response;
}

function channelControlActionDraftFields(action) {
  const normalized = String(action || "");
  if (["pin", "unpin"].includes(normalized)) return ["pinned"];
  if (normalized === "snooze") return ["snoozedUntil"];
  if (normalized === "pause") return ["waitReason"];
  if (normalized === "resume") return ["waitReason", "snoozedUntil"];
  return [];
}

function reconcileChannelLaneActionDrafts(conversationId, fields, expectedFieldRevisions, control = {}) {
  for (const field of fields || []) {
    reconcileChannelLaneActionDraft(
      conversationId,
      field,
      Number(expectedFieldRevisions?.[field] ?? -1),
      control,
    );
  }
}

function reconcileChannelLaneActionDraft(conversationId, field, expectedFieldRevision, control = {}) {
  if (!field) return;
  const draft = state.channelLaneDrafts.get(conversationId);
  if (!draft) return;
  if (Number(draft.fieldRevisions?.[field] ?? -1) !== expectedFieldRevision) return;
  const values = { ...(draft.values || {}) };
  if (field === "pinned") values.pinned = configBoolean(control.pinned, false);
  if (field === "priority") values.priority = String(control.priority ?? 50);
  if (field === "snoozedUntil") values.snoozedUntil = datetimeLocalValue(control.snoozed_until);
  if (field === "waitReason") values.waitReason = String(control.wait_reason || "");
  if (field === "operatorNote") values.operatorNote = String(control.operator_note || "");
  const dirtyFields = (draft.dirtyFields || []).filter((item) => item !== field);
  if (!dirtyFields.length) {
    state.channelLaneDrafts.delete(conversationId);
    return;
  }
  const fieldRevisions = { ...(draft.fieldRevisions || {}) };
  delete fieldRevisions[field];
  state.channelLaneDrafts.set(conversationId, { ...draft, values, dirtyFields, fieldRevisions });
}

async function saveChannelLaneControl(conversationId, root) {
  const submitted = state.channelLaneDrafts.get(conversationId) || rememberChannelLaneDraft(
    conversationId,
    root,
    { increment: false },
  );
  const submittedValues = channelLaneControlValues(root);
  const submittedFields = Array.from(new Set(submitted?.dirtyFields || []));
  const expectedFieldRevisions = Object.fromEntries(submittedFields.map((field) => [
    field,
    Number(submitted?.fieldRevisions?.[field] ?? -1),
  ]));
  const patch = {};
  if (submittedFields.includes("pinned")) patch.pinned = submittedValues.pinned;
  if (submittedFields.includes("priority")) patch.priority = Number(submittedValues.priority || 50);
  if (submittedFields.includes("waitReason")) patch.wait_reason = submittedValues.waitReason;
  if (submittedFields.includes("operatorNote")) patch.operator_note = submittedValues.operatorNote;
  if (submittedFields.includes("snoozedUntil")) {
    patch.snoozed_until = datetimeLocalToIso(submittedValues.snoozedUntil);
  }
  if (!Object.keys(patch).length) {
    state.channelLaneDrafts.delete(conversationId);
    return { status: "ok", message: "no_channel_control_changes" };
  }
  const response = await updateChannelControl(conversationId, {
    action: "update_control",
    patch,
  });
  reconcileChannelLaneActionDrafts(
    conversationId,
    submittedFields,
    expectedFieldRevisions,
    response.channel_state?.control || {},
  );
  renderChannelTaskLanes();
  return response;
}

async function queueChannelTestReply(conversationId, helpers = {}) {
  const id = String(conversationId || "").trim();
  if (!id) return { status: "error", message: "conversation_id_empty" };
  const scope = requireSelectedTalkerScope(id);
  if (!scope.ok) {
    helpers.update?.(100, scope.message);
    return scope;
  }
  const label = conversationLabel(id) || id;
  const defaultText = `【sidebar测试】这是一条发往 ${label} 的通道文本投递探针。`;
  const text = window.prompt("测试回复内容", defaultText);
  if (text === null) return { status: "cancelled_by_user", message: "已取消测试回复" };
  if (!String(text).trim()) return { status: "error", message: "测试回复内容不能为空" };
  helpers.update?.(30, "正在生成测试文本投递项");
  const payload = await api(`/api/channels/${encodeURIComponent(id)}/test-reply`, {
    method: "POST",
    body: JSON.stringify({
      text,
      talkers: scope.talkers,
      conversation_ids: scope.conversation_ids,
      require_scope: true,
    }),
  });
  if (payload.dispatch_mode === "confirm") state.activeStatus = "pending";
  const message = sidebarTestDispatchMessage("测试回复", payload);
  helpers.update?.(80, message);
  setStatusMessage(message);
  await refresh({ force: true });
  return payload;
}

async function queueChannelTestFile(conversationId, helpers = {}) {
  const id = String(conversationId || "").trim();
  if (!id) return { status: "error", message: "conversation_id_empty" };
  const scope = requireSelectedTalkerScope(id);
  if (!scope.ok) {
    helpers.update?.(100, scope.message);
    return scope;
  }
  helpers.update?.(18, "等待选择本地文件");
  const file = await chooseLocalFile();
  if (!file) return { status: "cancelled_by_user", message: "未选择文件" };
  const maxBytes = Number(state.data?.config?.file_max_bytes || 20 * 1024 * 1024);
  if (Number.isFinite(maxBytes) && maxBytes > 0 && file.size > maxBytes) {
    return { status: "error", message: `文件超过上限：${file.size} > ${maxBytes}` };
  }
  const caption = window.prompt("文件 caption（可留空，只投递文件）", "") ?? "";
  helpers.update?.(42, "正在读取文件内容");
  const contentBase64 = await readFileAsBase64(file);
  helpers.update?.(68, "正在生成文件发送审核项");
  const payload = await api(`/api/channels/${encodeURIComponent(id)}/test-file`, {
    method: "POST",
    body: JSON.stringify({
      caption,
      file: {
        name: file.name || "upload.bin",
        size: file.size,
        mime_type: file.type || "",
        content_base64: contentBase64,
      },
      talkers: scope.talkers,
      conversation_ids: scope.conversation_ids,
      require_scope: true,
    }),
  });
  if (payload.dispatch_mode === "confirm") state.activeStatus = "pending";
  const message = sidebarTestDispatchMessage("测试文件", payload);
  helpers.update?.(86, message);
  setStatusMessage(message);
  await refresh({ force: true });
  return payload;
}

function sidebarTestDispatchMessage(label, payload = {}) {
  if (payload.status === "blocked") {
    return `${label}被阻断：${payload.reason || payload.message || "发送范围未确认"}`;
  }
  if (payload.dispatch_mode === "auto") {
    const result = payload.send_result || {};
    const status = result.status || payload.status || "submitted";
    const reason = result.reason ? ` / ${result.reason}` : "";
    return `${label}已自动投递：${status}${reason}`;
  }
  return `${label}已进入发送审核：${payload.queue_id || ""}`;
}

function chooseLocalFile() {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.style.position = "fixed";
    input.style.left = "-9999px";
    let settled = false;
    const finish = (file) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(file || null);
    };
    input.addEventListener("change", () => finish(input.files?.[0] || null), { once: true });
    window.addEventListener("focus", () => {
      setTimeout(() => {
        if (!settled && !input.files?.length) finish(null);
      }, 350);
    }, { once: true });
    document.body.append(input);
    input.click();
  });
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("file_read_failed"));
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve(value.includes(",") ? value.split(",", 2)[1] : value);
    };
    reader.readAsDataURL(file);
  });
}

async function cleanupHiddenChannels() {
  if (!window.confirm("确定清理当前全部隐藏通道吗？")) {
    return { status: "cancelled_by_user", message: "用户取消清理隐藏通道" };
  }
  const payload = await api("/api/channels/cleanup-hidden", {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatusMessage(payload.note || "隐藏通道已清理");
  await refresh({ force: true });
  return payload;
}

async function ackBridge(bridgeId, status) {
  if (!bridgeId) return;
  const markingSent = status === "sent";
  const confirmed = window.confirm(
    markingSent
      ? "确认将该桥接项标记为已发送吗？\n\n仅处理仍处于待桥接（queued）的项，并且仅在微信侧已经核实送达时执行。worker 一旦开始发送（inflight）便无法取消，陈旧请求会被拒绝。"
      : "确认将该桥接项标记为失败吗？\n\n仅处理仍处于待桥接（queued）的项。worker 一旦开始发送（inflight）便无法取消，陈旧请求会被拒绝；人工失败是终态，系统不会自动重发。",
  );
  if (!confirmed) return { status: "cancelled_by_user", message: "用户取消桥接回执标记" };
  const payload = await api("/api/bridge/ack", {
    method: "POST",
    body: JSON.stringify({
      bridge_id: bridgeId,
      status,
      reason: status === "sent" ? "manual_sidebar_ack" : "manual_sidebar_failed",
    }),
  });
  if (payload.applied === false || payload.status === "conflict") {
    const effectiveStatus = bridgeStatusText(payload.effective_status || "") || "已变化";
    const message = `桥接项当前为${effectiveStatus}，人工标记未执行；已经开始的发送无法取消`;
    setStatusMessage(message);
    await refresh({ force: true });
    return { ...payload, status: "conflict", message };
  }
  setStatusMessage(status === "sent" ? "桥接项已标记为已发" : "桥接项已标记为失败");
  await refresh({ force: true });
  return payload;
}

async function retryBridge(bridgeId) {
  if (!bridgeId) return;
  if (!window.confirm("确认重投该桥接项吗？\n\n重投可能造成重复发送；请先核对微信侧没有实际送达。")) {
    return { status: "cancelled_by_user", message: "用户取消桥接重投" };
  }
  const payload = await api("/api/bridge/retry", {
    method: "POST",
    body: JSON.stringify({
      bridge_id: bridgeId,
      reviewer: "sidebar",
      note: "manual_sidebar_bridge_retry",
    }),
  });
  const nextId = payload.new_bridge_id ? `：${payload.new_bridge_id}` : "";
  setStatusMessage(`桥接项已重投${nextId}`);
  await refresh({ force: true });
  return payload;
}

async function runtimeCardAction(action, payload) {
  const result = await api(`/api/runtime-cards/${action}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setStatusMessage("技能/人设卡已更新");
  await refresh({ force: true });
  return result;
}

const PERSONA_CARD_FIELDS = [
  "#personaCardName",
  "#personaCardDescription",
  "#personaCardPersonality",
  "#personaCardScenario",
  "#personaCardMesExample",
  "#personaCardContent",
];
const TASK_CARD_FIELDS = ["#taskCardName", "#taskCardContent"];

async function savePersonaCard(event) {
  event.preventDefault();
  const submittedRevision = state.personaCardRevision;
  const name = $("#personaCardName").value.trim();
  const payload = {
    name,
    description: $("#personaCardDescription").value.trim(),
    personality: $("#personaCardPersonality").value.trim(),
    scenario: $("#personaCardScenario").value.trim(),
    mes_example: $("#personaCardMesExample").value.trim(),
    content: $("#personaCardContent").value.trim(),
  };
  const hasPersonaBody = ["description", "personality", "scenario", "mes_example", "content"]
    .some((key) => Boolean(payload[key]));
  if (!hasPersonaBody) {
    setStatusMessage("人物卡内容不能为空");
    return { status: "error", message: "人物卡内容不能为空" };
  }
  const result = await runtimeCardAction("save-persona", payload);
  if (state.personaCardRevision === submittedRevision) {
    PERSONA_CARD_FIELDS.forEach((selector) => {
      const node = $(selector);
      if (node) node.value = "";
    });
  }
  return result;
}

async function saveTaskCard(event) {
  event.preventDefault();
  const submittedRevision = state.taskCardRevision;
  const name = $("#taskCardName").value.trim();
  const content = $("#taskCardContent").value.trim();
  if (!content) {
    setStatusMessage("任务卡内容不能为空");
    return { status: "error", message: "任务卡内容不能为空" };
  }
  const result = await runtimeCardAction("save-task", { name, content });
  if (state.taskCardRevision === submittedRevision) {
    TASK_CARD_FIELDS.forEach((selector) => {
      const node = $(selector);
      if (node) node.value = "";
    });
  }
  return result;
}

async function saveChannelRuntimeOverride(event) {
  event.preventDefault();
  const conversationId = $("#channelOverrideConversationId").value.trim();
  if (!conversationId) {
    setStatusMessage("请先填写 conversation_id");
    return { status: "error", message: "conversation_id required" };
  }
  const personaId = $("#channelOverridePersonaSelect").value.trim();
  const skillIds = $("#channelOverrideSkillIds").value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (personaId) {
    await runtimeCardAction("set-channel-persona", { conversation_id: conversationId, card_id: personaId });
  } else {
    await runtimeCardAction("clear-channel-persona", { conversation_id: conversationId });
  }
  await runtimeCardAction("set-channel-skills", { conversation_id: conversationId, card_ids: skillIds });
  setStatusMessage(`通道卡片覆盖已保存：${conversationId}`);
  return { status: "ok" };
}

async function clearChannelPersonaOverride() {
  const conversationId = $("#channelOverrideConversationId").value.trim();
  if (!conversationId) {
    setStatusMessage("请先填写 conversation_id");
    return { status: "error", message: "conversation_id required" };
  }
  if (!window.confirm(`确定清除通道 ${conversationId} 的人物卡覆盖吗？`)) {
    return { status: "cancelled_by_user", message: "用户取消清除通道人物卡覆盖" };
  }
  const result = await runtimeCardAction("clear-channel-persona", { conversation_id: conversationId });
  setStatusMessage(`通道人物卡已恢复全局：${conversationId}`);
  return result;
}

async function clearChannelSkillsOverride() {
  const conversationId = $("#channelOverrideConversationId").value.trim();
  if (!conversationId) {
    setStatusMessage("请先填写 conversation_id");
    return { status: "error", message: "conversation_id required" };
  }
  if (!window.confirm(`确定清除通道 ${conversationId} 的技能覆盖吗？`)) {
    return { status: "cancelled_by_user", message: "用户取消清除通道技能覆盖" };
  }
  const result = await runtimeCardAction("clear-channel-skills", { conversation_id: conversationId });
  setStatusMessage(`通道技能已恢复全局：${conversationId}`);
  return result;
}

async function probeNow() {
  invalidateWechatProbeOverlay();
  const requestEpoch = state.wechatProbeRequestEpoch;
  renderWechatProbe(effectiveWechatProbe());
  renderProbeJson();
  const payload = await api("/api/wechat-probe", {
    method: "POST",
    body: JSON.stringify({}),
  });
  if (requestEpoch !== state.wechatProbeRequestEpoch) {
    return { ...payload, ignored: true, ignore_reason: "stale_probe_response" };
  }
  state.wechatProbeOverlay = { payload, requestEpoch, checkedAt: new Date().toISOString() };
  renderWechatProbe(payload);
  renderProbeJson();
  return payload;
}

async function probeDriverNow() {
  if (state.controlsDirty || state.controlsSaving) {
    setStatusMessage("请先保存发送控制，再探测后端");
    return { status: "blocked", reason: "unsaved_send_controls" };
  }
  const configFingerprint = driverProbeConfigFingerprint();
  if (!configFingerprint) {
    return { status: "blocked", reason: "send_config_unavailable" };
  }
  invalidateDriverProbeOverlay();
  const requestEpoch = state.driverProbeRequestEpoch;
  renderSendControlSummary();
  renderProbeJson();
  const payload = await api("/api/driver-probe", {
    method: "POST",
    body: JSON.stringify({}),
    timeoutMs: 10000,
  });
  const responseFingerprint = driverProbePayloadFingerprint(payload);
  if (
    requestEpoch !== state.driverProbeRequestEpoch
    || state.controlsDirty
    || configFingerprint !== driverProbeConfigFingerprint()
    || (responseFingerprint && responseFingerprint !== configFingerprint)
  ) {
    return { ...payload, ignored: true, ignore_reason: "stale_probe_response" };
  }
  const checkedAt = Date.now();
  state.driverProbeOverlay = {
    payload,
    requestEpoch,
    configFingerprint,
    checkedAt: new Date(checkedAt).toISOString(),
    expiresAt: checkedAt + DRIVER_PROBE_TTL_MS,
  };
  renderSendControlSummary();
  renderProbeJson();
  const summary = driverProbeSummary(payload);
  setStatusMessage(summary.text);
  return payload;
}

async function auditLocalResources(helpers = {}) {
  helpers.update?.(18, "正在采样本机 CPU / 内存 / GPU");
  const payload = await api("/api/resources/audit", {
    method: "POST",
    body: JSON.stringify({ manual: true, source: "sidebar" }),
  });
  helpers.update?.(68, "资源审计完成，正在刷新总台资源池");
  state.data = {
    ...(state.data || {}),
    resource_audit: payload,
  };
  await refresh({ force: true });
  helpers.update?.(92, "并发建议已写入总台视图");
  setStatusMessage("本机资源审计已完成");
  return payload;
}

async function exportDiagnosticsBundle(helpers = {}) {
  helpers.update?.(18, "正在收集诊断快照");
  try {
    const payload = await api("/api/diagnostics/export", {
      method: "POST",
      body: JSON.stringify({ limit: 80, persist: true }),
    });
    const createdAt = String(payload.created_at || new Date().toISOString()).replace(/[:.]/g, "-");
    const filename = `wechat-agent-diagnostics-${createdAt}.json`;
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.style.display = "none";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    const savedPath = payload.path ? `，已写入 ${payload.path}` : "";
    helpers.update?.(100, `诊断包已导出${savedPath}`);
    setStatusMessage(`诊断包已导出${savedPath}`);
    return payload;
  } catch (error) {
    setStatusMessage(`诊断导出失败：${error.message}`);
    return { status: "error", message: error.message, response: error.payload || null };
  }
}

async function probeRuntimeGpu(helpers = {}) {
  helpers.update?.(24, "正在审查 OCR/ASR 运行路径");
  const payload = {
    ocr_mode: runtimeMode($("#ocrModeSelect").value),
    asr_mode: runtimeMode($("#asrModeSelect").value),
    run_sample: true,
  };
  let result;
  try {
    result = await api("/api/runtime/probe", { method: "POST", body: JSON.stringify(payload) });
  } catch (error) {
    if (Number(error.httpStatus || 0) === 404 || String(error.message || "") === "not_found") {
      const message = "当前 sidebar 后端进程版本过旧：请重启 sidebar 后再审查 GPU，避免新前端连着旧 API。";
      helpers.update?.(100, message);
      setStatusMessage(message);
      throw new Error(message);
    }
    throw error;
  }
  state.data = { ...(state.data || {}), runtime_probe: result };
  state.probeExpanded = true;
  renderProbeJson();
  const message = runtimeProbeSummary(result, payload);
  helpers.update?.(88, message);
  setStatusMessage(message);
  return result;
}

function runtimeProbeSummary(result, requestedModes) {
  const parts = [
    runtimeEngineProbeText("OCR", result?.ocr?.health, requestedModes.ocr_mode, result?.ocr?.sample),
    runtimeEngineProbeText("ASR", result?.asr?.health, requestedModes.asr_mode, result?.asr?.sample),
  ];
  const gate = result?.gpu_gate || {};
  if (gate.max_parallel) {
    const activeSlots = Number(gate.active_slots ?? gate.active_in_process ?? 0);
    const localActive = Number(gate.active_in_process || 0);
    parts.push(`GPU队列：${activeSlots}/${Number(gate.max_parallel || 1)}（本进程${localActive}）`);
  }
  return parts.join(" / ");
}

function runtimeEngineProbeText(label, health = {}, requestedMode = "auto", sample = null) {
  const modeLabel = runtimeModeLabel(requestedMode);
  const sampleMetadata = sample?.metadata || {};
  const sampleChecked = Boolean(sample && sample.status === "ok");
  const gpuUsed = sampleChecked ? Boolean(sampleMetadata.gpu_used) : Boolean(health.gpu_used);
  const gpuRequired = requestedMode === "gpu" || Boolean(health.gpu_required);
  const gpuAvailable = Boolean(health.gpu_available);
  let stateText = "未启用GPU";
  if (gpuUsed) {
    stateText = "GPU已启用";
  } else if (gpuRequired) {
    stateText = "GPU不可用";
  } else if (!gpuAvailable) {
    stateText = "未检测到GPU";
  }
  let backendNote = "";
  if (sampleChecked && Array.isArray(sampleMetadata.backends) && sampleMetadata.backends.length) {
    backendNote = ` / backend=${sampleMetadata.backends.join(",")}`;
  } else if (sampleChecked && sampleMetadata.backend) {
    const modelNote = sampleMetadata.model ? ` model=${sampleMetadata.model}` : "";
    backendNote = ` / backend=${sampleMetadata.backend}${modelNote}`;
  } else if (health.backend) {
    backendNote = ` / health=${health.backend}`;
  }
  const reason = sample?.status === "error" ? sample.error : runtimeProbeReasonText(health);
  return `${label}：${modeLabel}/${stateText}${backendNote}${reason ? `（${reason}）` : ""}`;
}

function runtimeModeLabel(mode) {
  return {
    auto: "自动轻量档",
    gpu: "GPU档",
    cpu: "CPU档",
  }[runtimeMode(mode)] || "自动轻量档";
}

function runtimeProbeReasonText(health = {}) {
  const backend = String(health.backend || "");
  const detail = String(health.detail || "");
  if (backend === "paddleocr_gpu_required_unavailable" || detail.includes("CUDA-enabled PaddleOCR")) {
    return "缺少CUDA版Paddle/PaddleOCR";
  }
  if (backend === "local_asr_gpu_required_unavailable" || detail.includes("GPU ASR required")) {
    return "ASR CUDA运行时不可用";
  }
  if (!health.available && detail) {
    return detail;
  }
  return "";
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
    ...extra,
  };
}

function agentConversationScopePayload() {
  const talkers = talkerIds();
  if (!talkers.length) return {};
  return {
    talkers,
    conversation_ids: conversationIdsForTalkers(talkers),
  };
}

function conversationIdsForTalkers(talkers) {
  const wanted = new Set((Array.isArray(talkers) ? talkers : []).map((item) => String(item || "").trim()).filter(Boolean));
  if (!wanted.size) return [];
  const channels = Array.isArray(state.data?.channels?.items) ? state.data.channels.items : [];
  const ids = [];
  for (const channel of channels) {
    const aliases = [
      channel.conversation_id,
      channel.conversation_key,
      channel.chat_title,
      ...(Array.isArray(channel.sender_wechat_ids) ? channel.sender_wechat_ids : []),
      ...(Array.isArray(channel.sender_names) ? channel.sender_names : []),
    ].map((item) => String(item || "").trim()).filter(Boolean);
    if (aliases.some((item) => wanted.has(item))) ids.push(String(channel.conversation_id || "").trim());
  }
  return [...new Set(ids.filter(Boolean))];
}

function requireSelectedTalkerScope(conversationId = "") {
  const id = String(conversationId || "").trim();
  const talkers = talkerIds();
  if (!talkers.length) {
    const message = "请先在 WeFlow talkers 中选择至少一个本地通道";
    setStatusMessage(message);
    return { ok: false, status: "blocked", reason: "send_scope_required", message, talkers: [], conversation_ids: [] };
  }
  const conversationIds = conversationIdsForTalkers(talkers);
  if (id && !conversationIds.includes(id)) {
    const message = "当前通道不在已选 talkers 范围内，已阻止发送";
    setStatusMessage(message);
    return {
      ok: false,
      status: "blocked",
      reason: "send_scope_mismatch",
      message,
      talkers,
      conversation_ids: conversationIds,
    };
  }
  return { ok: true, talkers, conversation_ids: conversationIds };
}

async function weflowAction(action, extra = {}, helpers = {}) {
  helpers.update?.(24, `WeFlow ${action} 请求已发送`);
  try {
    const payload = await api(`/api/weflow/${action}`, {
      method: "POST",
      body: JSON.stringify(weflowPayload(extra)),
    });
    helpers.update?.(78, `WeFlow ${action} 正在刷新状态`);
    showWeFlowStatusPayload(payload);
    setStatusMessage(`WeFlow ${action} 完成`);
    await refresh({ force: true });
    return payload;
  } catch (error) {
    const payload = { status: "error", action, message: error.message, response: error.payload || null };
    showWeFlowStatusPayload(payload);
    setStatusMessage(`WeFlow ${action} 失败：${error.message}`);
    await refresh({ force: true });
    return payload;
  }
}

async function runAgentTick(helpers = {}) {
  const scope = requireSelectedTalkerScope();
  if (!scope.ok) {
    helpers.update?.(100, scope.message);
    return scope;
  }
  helpers.update?.(18, "正在读取当前会话文件");
  try {
    const payload = await api("/api/agent/tick", {
      method: "POST",
      body: JSON.stringify({ loops: 1, ...agentConversationScopePayload() }),
    });
    const agent = payload.agent || {};
    const snapshot = payload.session_snapshot?.after || {};
    const processed = Number(agent.processed_count ?? payload.processed_count ?? 0);
    const conversationCount = Number(snapshot.conversation_count || 0);
    const pendingCount = Number(snapshot.pending_user_count || 0);
    const blockedCount = Number(snapshot.blocked_pending_user_count || 0);
    const openingCount = Number(snapshot.opening_greeting_count || 0);
    const tickMessage = processed > 0
      ? `已处理 ${processed} 条消息，逐通道聚合 ${conversationCount} 个通道`
      : `没有新事件；逐通道聚合 ${conversationCount} 个通道，待接 ${pendingCount} 条，阻断 ${blockedCount} 条，开场 ${openingCount} 通道`;
    helpers.update?.(88, tickMessage);
    setStatusMessage(`对话 Agent 完成：${processed > 0 ? `处理 ${processed} 条消息` : "没有新事件"}`);
    if (state.data) {
      if (payload.agent_state) state.data.agent = payload.agent_state;
      if (payload.task_manager) state.data.task_manager = payload.task_manager;
      if (payload.channels) state.data.channels = payload.channels;
    }
    await refresh({ force: true });
    return payload;
  } catch (error) {
    setStatusMessage(`对话 Agent 失败：${error.message}`);
    await refresh({ force: true });
    return { status: "error", message: error.message, response: error.payload || null };
  }
}

async function runAgentWorkerAction(action, helpers = {}) {
  const starting = action === "start";
  const scope = starting ? requireSelectedTalkerScope() : { ok: true };
  if (!scope.ok) {
    helpers.update?.(100, scope.message);
    return scope;
  }
  helpers.update?.(18, starting ? "正在启动连续接话" : "正在停止连续接话");
  try {
    const payload = await api(`/api/agent/${action}`, {
      method: "POST",
      body: JSON.stringify(starting ? { interval_seconds: 2, loops: 1, ...agentConversationScopePayload() } : {}),
    });
    const worker = payload.worker || payload.agent_state?.worker || {};
    if (payload.status === "blocked") {
      const message = payload.message || payload.reason || "连续接话已被阻断";
      helpers.update?.(100, message);
      setStatusMessage(`连续接话启动被阻断：${message}`);
      if (state.data) {
        if (payload.agent_state) state.data.agent = payload.agent_state;
        if (payload.task_manager) state.data.task_manager = payload.task_manager;
      }
      await refresh({ force: true });
      return payload;
    }
    helpers.update?.(82, starting ? "连续接话状态已启动" : "连续接话停止信号已发送");
    setStatusMessage(
      starting
        ? `连续接话：${worker.running ? "运行中" : "已请求启动"}`
        : `连续接话：${worker.running ? "停止中" : "已停止"}`,
    );
    if (state.data) {
      if (payload.agent_state) state.data.agent = payload.agent_state;
      else state.data.agent = { ...(state.data.agent || {}), worker };
      if (payload.task_manager) state.data.task_manager = payload.task_manager;
    }
    await refresh({ force: true });
    return payload;
  } catch (error) {
    setStatusMessage(`连续接话${starting ? "启动" : "停止"}失败：${error.message}`);
    await refresh({ force: true });
    return { status: "error", message: error.message, response: error.payload || null };
  }
}

async function weflowDiscoverSessions(helpers = {}) {
  helpers.update?.(22, "正在向 WeFlow 请求会话列表");
  try {
    const payload = await api("/api/weflow/discover-sessions", {
      method: "POST",
      body: JSON.stringify(weflowPayload({ limit: 100 })),
    });
    if (payload.status === "ok" && payload.sessions) {
      renderSessionList(payload.sessions);
      showWeFlowStatusPayload(payload);
      setStatusMessage(`发现 ${payload.count} 个会话`);
      helpers.update?.(82, `已发现 ${payload.count} 个会话`);
      await refresh({ force: true });
      return payload;
    } else {
      showWeFlowStatusPayload(payload);
      setStatusMessage("会话发现失败");
      await refresh({ force: true });
      return payload;
    }
  } catch (error) {
    const payload = { status: "error", action: "discover-sessions", message: error.message, response: error.payload || null };
    showWeFlowStatusPayload(payload);
    setStatusMessage(`会话发现失败：${error.message}`);
    await refresh({ force: true });
    return payload;
  }
}

function renderSessionList(sessions) {
  const list = $("#weflowSessionList");
  if (!sessions || !sessions.length) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  list.innerHTML = sessions
    .map(
      (s) => `
    <div class="session-item${isBlockedWeFlowSession(s) ? " blocked" : ""}" data-session-id="${escapeHtml(s.id || "")}" data-blocked="${isBlockedWeFlowSession(s) ? "true" : "false"}">
      <div>
        <div class="session-item-name">${escapeHtml(s.name || s.id || "（无名称）")}</div>
        <div class="session-item-id">${escapeHtml(s.id || "")}${isBlockedWeFlowSession(s) ? ` / ${escapeHtml(weflowBlockedSessionText(s))}` : ""}</div>
      </div>
    </div>
  `
    )
    .join("");
  list.querySelectorAll(".session-item").forEach((item) => {
    item.addEventListener("click", () => {
      if (item.dataset.blocked === "true") return;
      const sessionId = item.dataset.sessionId;
      if (sessionId) {
        addTalker(sessionId);
        list.hidden = true;
      }
    });
  });
}

function isBlockedWeFlowSession(session) {
  return (
    String(session?.channel_registration_status || "").trim() === "blocked" ||
    Boolean(String(session?.channel_blocked_reason || "").trim())
  );
}

function weflowBlockedSessionText(session) {
  const reason = String(session?.channel_blocked_reason || "").trim();
  const labels = {
    private_contact_explicitly_not_friend: "非好友已阻断",
    private_contact_unknown_or_unidentified: "未知私聊已阻断",
  };
  return labels[reason] || "通道已阻断";
}

function showWeFlowStatusText(text, mode = "action") {
  state.weflowStatusMode = mode;
  $("#weflowStatusBox").textContent = text;
}

function showWeFlowStatusPayload(payload, mode = "action") {
  showWeFlowStatusText(JSON.stringify(compactPayload(payload, 5000), null, 2), mode);
}

function setWeFlowHelpVisible(visible) {
  const popover = $("#weflowHelpPopover");
  const button = $("#weflowHelpButton");
  if (!popover || !button) return;
  popover.hidden = !visible;
  button.setAttribute("aria-expanded", visible ? "true" : "false");
}

function renderWeFlowHistory(history) {
  const box = $("#weflowHistoryBox");
  const content = $("#weflowHistoryContent");
  const items = Array.isArray(history) ? history : [];
  if (!items.length) {
    box.hidden = true;
    content.innerHTML = "";
    return;
  }
  box.hidden = false;
  content.innerHTML = items
    .map(
      (entry) => `
    <div class="history-entry">
      <div class="history-entry-time">${escapeHtml(entry.time || "")}</div>
      <div class="history-entry-action">${escapeHtml(entry.action || "")}</div>
      <div class="history-entry-result">${escapeHtml(weflowHistorySummary(entry))}</div>
    </div>
  `
    )
    .join("");
}

function weflowHistorySummary(entry) {
  const result = entry?.result && typeof entry.result === "object" ? entry.result : {};
  const parts = [];
  const talkers = Array.isArray(result.backfilled_talkers) ? result.backfilled_talkers : [];
  if (talkers.length) parts.push(`回填对象=${talkers.length}个`);
  if (result.workers !== undefined) parts.push(`workers=${result.workers}`);
  const source = result.source && typeof result.source === "object" ? result.source : {};
  const pull = result.pull && typeof result.pull === "object" ? result.pull : {};
  const imported = pull.import && typeof pull.import === "object" ? pull.import : {};
  if (source.status) parts.push(`源=${source.status}`);
  if (source.scanned_count !== undefined) parts.push(`扫描页=${source.scanned_count}`);
  if (source.appended_count !== undefined) parts.push(`源新增=${source.appended_count}`);
  if (imported.appended_count !== undefined) parts.push(`导入后端=${imported.appended_count}`);
  if (pull.processed_count !== undefined) parts.push(`写入对话=${pull.processed_count}`);
  if (result.message || result.error) parts.push(String(result.message || result.error));
  const summary = String(entry?.summary || "").trim();
  return parts.length ? parts.join(" / ") : (summary || JSON.stringify(compactPayload(result || entry, 300)));
}

async function weflowBackfill(helpers = {}) {
  const talkers = splitComma($("#weflowTalkers").value);
  if (!talkers.length) {
    const payload = { status: "error", action: "backfill", message: "回填历史需要在 Talkers 填写要初始化的会话 id（不能为空）。" };
    showWeFlowStatusPayload(payload);
    setStatusMessage("回填历史需要指定 Talkers");
    return payload;
  }
  if (!window.confirm(`将从头拉取以下会话的历史消息作为上下文（不会回复旧消息）：\n${talkers.join(", ")}`)) {
    return { status: "cancelled_by_user", message: "用户取消回填历史" };
  }
  helpers.update?.(18, "已确认，正在创建历史回填任务");
  try {
    const payload = await api("/api/weflow/backfill", {
      method: "POST",
      body: JSON.stringify(weflowPayload({ talkers, context_only: true, force_context_only: true })),
    });
    showWeFlowStatusPayload(payload);
    if (payload.status === "started") {
      const jobId = payload.backfill_job?.job_id || "";
      if (jobId && helpers.task?.id) {
        state.backfillTaskByJobId.set(jobId, helpers.task.id);
      }
      helpers.update?.(30, "历史回填已创建，等待后端进度");
      setStatusMessage("WeFlow backfill started");
      await refresh({ force: true });
      return waitForBackfillCompletion(jobId, helpers);
    }
    setStatusMessage(`WeFlow 回填历史完成（${(payload.backfilled_talkers || []).length} 个会话）`);
    await refresh({ force: true });
    return payload;
  } catch (error) {
    const payload = { status: "error", action: "backfill", message: error.message, response: error.payload || null };
    showWeFlowStatusPayload(payload);
    setStatusMessage(`WeFlow 回填历史失败：${error.message}`);
    await refresh({ force: true });
    return payload;
  }
}

async function waitForBackfillCompletion(jobId, helpers = {}) {
  if (!jobId) {
    helpers.update?.(70, "回填任务已创建，等待下一次状态刷新");
    return { status: "started", message: "backfill job started" };
  }
  const startedAt = Date.now();
  let lastProgressAt = Date.now();
  let lastSignature = "";
  let missingJobPolls = 0;
  while (true) {
    await sleep(BACKFILL_POLL_INTERVAL_MS);
    const weflow = await api("/api/weflow/status");
    state.data = { ...(state.data || {}), weflow };
    renderWeFlow(weflow);
    const job = weflow.backfill_job || {};
    const status = String(job.status || "");
    const sameJob = String(job.job_id || "") === String(jobId);
    if (!sameJob || !status || status === "idle") {
      missingJobPolls += 1;
      if (missingJobPolls >= 3) {
        return {
          status: "interrupted",
          message: "历史回填任务状态已丢失，前端已解除按钮锁；如后端仍在运行，可使用取消回填。",
          backfill_job: job,
        };
      }
    } else {
      missingJobPolls = 0;
    }
    const progress = backfillProgress(job);
    helpers.update?.(progress.percent, progress.text);
    const signature = JSON.stringify([
      status,
      job.updated_at || "",
      job.running || false,
      job.cancel_requested || false,
      job.progress || {},
    ]);
    if (signature !== lastSignature) {
      lastSignature = signature;
      lastProgressAt = Date.now();
    }
    if (["completed", "cancelled", "error", "interrupted"].includes(status)) {
      const result = weflow.last_backfill || job.result || job;
      showWeFlowStatusPayload(result);
      return {
        ...(typeof result === "object" && result ? result : {}),
        status: job.status === "completed" ? (result.status || "ok") : job.status,
        backfill_job: job,
      };
    }
    if (Date.now() - lastProgressAt > BACKFILL_STALE_TIMEOUT_MS) {
      return {
        status: "interrupted",
        message: "历史回填长时间没有状态进展，前端已解除按钮锁；后端若仍运行可使用取消回填。",
        backfill_job: job,
      };
    }
    if (Date.now() - startedAt > BACKFILL_MAX_WAIT_MS) {
      return {
        status: "interrupted",
        message: "历史回填超过前端等待上限，前端已解除按钮锁；后端若仍运行可在状态面板继续观察或取消。",
        backfill_job: job,
      };
    }
  }
}

function backfillProgress(job) {
  const progress = job?.progress || {};
  const status = String(job?.status || "");
  if (["completed", "cancelled", "error", "interrupted"].includes(status)) {
    return { percent: 100, text: `历史回填${backfillStatusText(status)}` };
  }
  if (status === "finalizing") {
    return { percent: 94, text: "历史回填正在收尾" };
  }
  const pageCount = Number(progress.page_count || 0);
  const scanned = Number(progress.scanned_count || 0);
  const appended = Number(progress.appended_count || 0);
  const processed = Number(progress.processed_count || 0);
  const session = progress.current_session_id || (job?.talkers || [])[0] || "";
  const estimated = 30 + Math.min(58, pageCount * 8 + scanned * 1.5 + appended * 2);
  const text = [
    "历史回填运行中",
    session ? `会话 ${session}` : "",
    pageCount ? `页 ${pageCount}` : "",
    scanned ? `扫描 ${scanned}` : "",
    appended ? `写入 ${appended}` : "",
    processed ? `处理 ${processed}` : "",
  ].filter(Boolean).join(" / ");
  return { percent: Math.max(30, Math.min(88, estimated)), text };
}

function syncBackfillTask(job) {
  const jobId = String(job?.job_id || "");
  if (!jobId || !state.backfillTaskByJobId.has(jobId)) return;
  const taskId = state.backfillTaskByJobId.get(jobId);
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task || !["queued", "running"].includes(task.status)) return;
  const progress = backfillProgress(job);
  const terminal = ["completed", "cancelled", "error", "interrupted"].includes(String(job.status || ""));
  updateTask(taskId, {
    status: terminal ? (job.status === "completed" ? "completed" : job.status === "cancelled" ? "cancelled" : "failed") : task.status,
    progress: terminal ? 100 : progress.percent,
    phase: progress.text,
    finishedAt: terminal ? new Date().toISOString() : task.finishedAt,
  });
}

function backfillStatusText(status) {
  return {
    completed: "完成",
    cancelled: "已取消",
    error: "失败",
    interrupted: "已中断",
  }[status] || status;
}

async function weflowCancelBackfill(helpers = {}) {
  helpers.update?.(28, "正在发送取消回填信号");
  const payload = await api("/api/weflow/cancel-backfill", {
    method: "POST",
    body: JSON.stringify({}),
  });
  showWeFlowStatusPayload(payload);
  setStatusMessage(payload.status === "cancel_requested" ? "WeFlow backfill cancel requested" : "No active backfill job");
  await refresh({ force: true });
  return payload;
}

async function weflowDependencies(helpers = {}) {
  helpers.update?.(30, "正在检查可选依赖");
  const payload = await api("/api/weflow/dependencies", {
    method: "POST",
    body: JSON.stringify({}),
  });
  showWeFlowStatusPayload(payload);
  setStatusMessage("WeFlow 依赖检查完成");
  await refresh({ force: true });
  helpers.update?.(84, "依赖检查结果已刷新");
  return payload;
}

async function weflowInstallDeps(helpers = {}) {
  if (!window.confirm("将安装轻量 OCR/ASR 与文档依赖；GPU 依赖不在默认安装内。")) {
    return { status: "cancelled_by_user", message: "用户取消依赖安装" };
  }
  helpers.update?.(18, "正在安装可选依赖");
  const payload = await api("/api/weflow/install-deps", {
    method: "POST",
    body: JSON.stringify({ confirm_install: true }),
    timeoutMs: 15 * 60 * 1000,
  });
  showWeFlowStatusPayload(compactPayload(payload, 6000));
  setStatusMessage("WeFlow 依赖安装完成");
  await refresh({ force: true });
  return payload;
}

async function nativeMigrationProbe(helpers = {}) {
  helpers.update?.(18, "正在检查微信版本和 Native HTTP");
  try {
    const payload = await api("/api/native/migration-probe", {
      method: "POST",
      body: JSON.stringify({ persist: true, include_cleanup_sizes: true }),
    });
    helpers.update?.(78, nativeMigrationSummary(payload));
    showWeFlowStatusPayload(compactPayload(payload, 7000));
    state.data = { ...(state.data || {}), native_migration: payload };
    setStatusMessage(nativeMigrationSummary(payload));
    await refresh({ force: true });
    return payload;
  } catch (error) {
    const payload = { status: "error", action: "native-migration-probe", message: error.message, response: error.payload || null };
    showWeFlowStatusPayload(payload);
    setStatusMessage(`Native 探测失败：${error.message}`);
    return payload;
  }
}

function nativeMigrationSummary(payload) {
  const gate = payload?.version_gate?.gate || "unknown";
  const version = payload?.version_gate?.best_version || "未知版本";
  const http = payload?.http_status?.status || "unknown";
  const candidates = payload?.message_scan?.candidate_count ?? 0;
  const blockers = Array.isArray(payload?.blockers) ? payload.blockers.length : 0;
  const deploy = payload?.deploy_manifest?.status || "unknown";
  const saved = payload?.report_path ? " / 已保存报告" : "";
  return `Native：${payload?.status || "unknown"} / 部署 ${deploy} / 版本 ${version}(${gate}) / HTTP ${http} / 路径候选 ${candidates} / 阻断 ${blockers}${saved}`;
}

function splitComma(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function actionButton(label, className, handler, meta = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.title = meta.tooltip || meta.label || label;
  button.dataset.taskScope = meta.scope || "global";
  button.addEventListener("click", () => {
    runTask(
      {
        label: meta.label || label,
        category: meta.category || "操作",
        scope: meta.scope || "global",
        scopeLabel: meta.scopeLabel,
        target: meta.target || "",
        persist: meta.persist,
        button,
      },
      (helpers) => handler(helpers),
    );
  });
  syncTaskButtonLock(button);
  return button;
}

function simpleButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    handler(event);
  });
  return button;
}

function bindTaskButton(selector, meta, handler) {
  const button = $(selector);
  if (!button) return;
  const initial = typeof meta === "function" ? null : meta;
  if (initial?.scope) button.dataset.taskScope = initial.scope;
  button.addEventListener("click", (event) => {
    event.preventDefault();
    const resolved = typeof meta === "function" ? meta(event) : meta;
    event.currentTarget.dataset.taskScope = resolved?.scope || "global";
    runTask(
      {
        ...(resolved || {}),
        button: event.currentTarget,
      },
      (helpers) => handler(helpers, event),
    );
  });
  syncTaskButtonLock(button);
}

function syncTaskButtonLocks() {
  $$('button[data-task-scope]').forEach(syncTaskButtonLock);
}

function syncTaskButtonLock(button) {
  if (!button) return;
  const scope = String(button.dataset.taskScope || "global");
  const shouldLock = state.historyResetPending || Boolean(activeTaskForScope(scope));
  if (shouldLock) {
    if (!button.dataset.taskLocked && !button.dataset.taskScopeLocked) {
      button.dataset.taskScopeWasDisabled = button.disabled ? "1" : "0";
      button.dataset.taskScopeLocked = "1";
    }
    button.disabled = true;
    return;
  }
  if (button.dataset.taskScopeLocked) {
    const wasDisabled = button.dataset.taskScopeWasDisabled === "1";
    delete button.dataset.taskScopeLocked;
    delete button.dataset.taskScopeWasDisabled;
    if (!button.dataset.taskLocked) button.disabled = wasDisabled;
  }
}

function initBoundedTooltips() {
  const tooltip = document.createElement("div");
  tooltip.className = "tooltip-popup";
  tooltip.hidden = true;
  document.body.append(tooltip);

  const show = (button) => {
    const text = button.dataset.tooltip || "";
    if (!text) return;
    tooltip.textContent = text;
    tooltip.hidden = false;
    const shellRect = $(".shell").getBoundingClientRect();
    const buttonRect = button.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const gap = 10;
    const padding = 10;
    const minLeft = shellRect.left + padding;
    const maxLeft = shellRect.right - tooltipRect.width - padding;
    const idealLeft = buttonRect.left + buttonRect.width / 2 - tooltipRect.width / 2;
    const left = Math.max(minLeft, Math.min(maxLeft, idealLeft));
    const placeBelow = buttonRect.top - tooltipRect.height - gap < padding;
    const top = placeBelow ? buttonRect.bottom + gap : buttonRect.top - tooltipRect.height - gap;
    const arrowX = buttonRect.left + buttonRect.width / 2 - left;
    tooltip.style.left = `${Math.round(left)}px`;
    tooltip.style.top = `${Math.round(top)}px`;
    tooltip.dataset.placement = placeBelow ? "bottom" : "top";
    tooltip.style.setProperty("--tip-x", `${Math.round(Math.max(10, Math.min(tooltipRect.width - 10, arrowX)))}px`);
  };

  const hide = () => {
    tooltip.hidden = true;
  };

  document.addEventListener("mouseover", (event) => {
    const button = event.target.closest("button[data-tooltip]");
    if (button) show(button);
  });
  document.addEventListener("focusin", (event) => {
    const button = event.target.closest("button[data-tooltip]");
    if (button) show(button);
  });
  document.addEventListener("mouseout", (event) => {
    if (event.target.closest("button[data-tooltip]")) hide();
  });
  document.addEventListener("focusout", (event) => {
    if (event.target.closest("button[data-tooltip]")) hide();
  });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
}

function setActiveStatus(status) {
  state.activeStatus = status;
  $$(".metric").forEach((item) => item.classList.toggle("active", item.dataset.status === status));
  renderQueue();
}

function setActivePage(page) {
  state.activePage = page;
  $$(".page-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
  $("#overviewPage").hidden = page !== "overview";
  $("#sendPage").hidden = page !== "send";
  $("#weflowPage").hidden = page !== "weflow";
  $("#tasksPage").hidden = page !== "tasks";
  $("#historyPage").hidden = page !== "history";
  $("#diagnosticsPage").hidden = page !== "diagnostics";
  if (page === "diagnostics") {
    loadModelConfig();
    loadKeyPool();
  }
  setTimeout(repositionTaskProgressPopovers, 0);
}

function setActivePanel(panel) {
  state.activePanel = panel;
  $$(".bookmark-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panel);
  });
  $("#queuePanel").hidden = panel !== "queue";
  $("#bridgePanel").hidden = panel !== "bridge";
  $("#runtimePanel").hidden = panel !== "runtime";
  setTimeout(repositionTaskProgressPopovers, 0);
}

function setMode(mode) {
  const normalized = normalizeSendMode(mode, "");
  $$("#sendModeSegment button").forEach((button) => {
    const active = button.dataset.mode === normalized;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", active ? "true" : "false");
  });
  return normalized;
}

function currentMode(fallback = "dry_run") {
  return normalizeSendMode($("#sendModeSegment button.active")?.dataset.mode, fallback);
}

function markControlsDirty() {
  invalidateDriverProbeOverlay();
  state.controlsRevision += 1;
  state.controlsDirty = true;
  setDirtyIndicator("dirty");
  renderSendControlSummary();
  syncDriverProbeAvailability();
}

function syncDriverProbeAvailability() {
  const button = $("#driverProbeButton");
  if (!button || button.dataset.taskLocked || button.dataset.taskScopeLocked) return;
  button.disabled = state.controlsDirty || state.controlsSaving || state.historyResetPending;
}

function setDirtyIndicator(status) {
  const button = $("#saveControls");
  if (!button) return;
  button.disabled = status === "saving"
    || state.historyResetPending
    || Boolean(button.dataset.taskLocked)
    || Boolean(button.dataset.taskScopeLocked);
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

function emptyNode(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function shortTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime()) && raw.includes("T")) {
    const pad = (number) => String(number).padStart(2, "0");
    return `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
  }
  const afterT = raw.includes("T") ? raw.split("T")[1] : raw;
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

function queueStatusText(status, reason = "") {
  if (status === "sent" && String(reason || "").includes("dry_run_not_delivered")) {
    return "演练完成（未投递）";
  }
  return {
    pending: "待审核",
    approved: "已通过",
    rejected: "已拒绝",
    accepted: "端口已接收（未验证）",
    sent: "已发送",
    failed: "失败",
    queued: "已入桥",
    queued_to_bridge: "已入非前台桥",
    dry_run: "演练",
    queued_for_confirm: "待审核",
    skipped: "跳过",
  }[status] || status || "";
}

function bridgeStatusText(status, reason = "") {
  if (status === "sent" && String(reason || "").includes("dry_run_not_delivered")) {
    return "演练完成（未投递）";
  }
  return {
    queued: "待桥接",
    accepted: "端口已接收（未验证）",
    sent: "已确认发送",
    failed: "发送失败",
    blocked: "已阻断",
  }[status] || status || "";
}

function bridgeWorkerSummary(worker) {
  if (!worker || typeof worker !== "object") return "";
  const source = String(worker.source || "");
  const running = worker.running ? "running" : "stopped";
  const pid = worker.pid ? `pid=${worker.pid}` : "";
  const backend = worker.backend_name ? `backend=${worker.backend_name}` : "";
  const config = bridgeWorkerConfigText(worker.config_status || "", worker.config_match);
  return ["worker", running, source, pid, backend, config].filter(Boolean).join(" ");
}

function bridgeWorkerConfigText(status, match) {
  if (status === "matched" || match === true) return "config=matched";
  if (status === "stale" || match === false) return "config=stale";
  if (status === "unknown_legacy_lock") return "config=legacy-unknown";
  if (status === "not_running") return "";
  return status ? `config=${status}` : "";
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
    bridge_outbox_available: "bridge_outbox 可入队",
    bridge_outbox_configured_disabled: "bridge_outbox 已配置，发送未启用",
    bridge_outbox_dry_run_backend: "bridge_outbox 使用 dry-run 后端，不会投递到微信",
    bridge_outbox_weflow_http_unavailable: "bridge_outbox 使用 WeFlow HTTP，但本地发送端口未就绪",
    bridge_outbox_weflow_token_missing: "bridge_outbox 使用 WeFlow HTTP，但缺少 WEFLOW_API_TOKEN",
    bridge_outbox_weflow_send_not_supported: "bridge_outbox 使用 WeFlow HTTP，但当前 WeFlow 未实现非前台发送",
    bridge_outbox_wechat_native_http_unavailable: "bridge_outbox 使用本地微信 Native HTTP，但本地服务未就绪或未登录",
    bridge_outbox_wechat_native_accepted_unverified: "bridge_outbox 使用本地微信 Native HTTP，端口可接收；sent 由回读确认",
    bridge_outbox_ready: "bridge_outbox 已启用（投递中）",
    bridge_outbox_worker_down: "bridge_outbox 已启用，但投递进程未运行",
    bridge_outbox_worker_down_backlog: "bridge_outbox 投递进程未运行，有待发消息积压",
    bridge_outbox_backend_probe_deferred: "worker 运行中；后端健康等待主动探测",
    bridge_outbox_worker_config_unknown: "bridge_outbox 投递进程运行中，但配置签名未知",
    bridge_outbox_worker_stale_config: "bridge_outbox 投递进程运行中，但启动配置已过期",
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
    unchecked: "尚未主动探测",
    unknown: "未知",
  }[value] || value;
}

function actionText(action) {
  return {
    confirm_approve: "发送审核通过",
    confirm_reject: "发送审核拒绝",
    confirm_remove: "发送审核移除",
    confirm_send_attempt: "发送提交",
    confirm_send_blocked: "发送阻断",
    bridge_ack_sync: "桥接回执同步",
    bridge_retry: "桥接重投",
    ledger_sync_failed: "账本同步失败",
    ledger_sync_recovered: "账本同步已恢复",
    approve: "通过",
    reject: "拒绝",
    "send-approved": "发送",
  }[action] || action;
}

function channelDisplayName(channel) {
  const candidates = [
    channel.chat_title,
    ...(Array.isArray(channel.sender_names) ? channel.sender_names : []),
  ];
  const name = candidates
    .map((item) => String(item || "").trim())
    .find((item) =>
      item &&
      !looksLikePlaceholderContactName(item) &&
      !looksLikeInternalConversationId(item) &&
      !looksLikeMojibakeText(item)
    );
  if (name) return name;
  return channel.conversation_type === "group" ? "未命名群聊" : "未命名联系人";
}

function channelDisplayHint(channel) {
  const type = conversationTypeText(channel.conversation_type || "") || "会话";
  const sources = Array.isArray(channel.source_names) ? channel.source_names.filter(Boolean).length : 0;
  return sources ? `${type} / 已绑定消息源 ${sources}` : `${type} / 已隐藏内部会话 ID`;
}

function channelByConversationId(conversationId) {
  const id = String(conversationId || "").trim();
  const items = state.data?.channels?.items || [];
  return items.find((channel) => String(channel.conversation_id || "").trim() === id) || null;
}

function conversationLabel(conversationId) {
  const id = String(conversationId || "").trim();
  if (!id) return "";
  const channel = channelByConversationId(id);
  if (!channel) return id;
  const name = channelDisplayName(channel);
  return name && name !== id ? `${name} / ${id}` : id;
}

function looksLikeInternalConversationId(value) {
  const text = String(value || "").trim();
  return (
    /^wxid_/i.test(text) ||
    /^gh_/i.test(text) ||
    /@chatroom$/i.test(text) ||
    /^[a-f0-9]{20,}$/i.test(text) ||
    /^private:[a-f0-9]/i.test(text) ||
    /^group:[a-f0-9]/i.test(text)
  );
}

function looksLikeMojibakeText(value) {
  return /[锟閿缁閻閸濞鐏閺闂娑閹涔]/.test(String(value || ""));
}

function looksLikePlaceholderContactName(value) {
  return ["unknown", "unknown contact", "未知", "未知联系人", "system", "none", "null"].includes(
    String(value || "").trim().toLowerCase(),
  );
}

function reasonSummary(reasons) {
  const labels = {
    probe_fragment: "探测碎片",
    untrusted_channel: "未信任通道",
    mojibake: "乱码标题",
    tool_window: "工具窗口",
    empty_title: "空标题",
  };
  return Object.entries(reasons)
    .map(([key, count]) => `${labels[key] || key} ${count}`)
    .join("，");
}

const keyPoolState = {
  keys: [],
  keyFile: "",
  writable: false,
  loading: false,
  requestEpoch: 0,
  loadPromise: null,
  inputRevision: 0,
};

const MODEL_QUICK_OPTIONS = [
  { value: "gpt-5.4", label: "gpt 5.4" },
  { value: "gpt-5.4-mini", label: "gpt 5.4 mini" },
  { value: "gpt-5.5", label: "gpt 5.5" },
  { value: "deepseek-v4-flash", label: "deepseek v4 flash" },
  { value: "deepseek-v4-pro", label: "deepseek v4 pro" },
];

const modelConfigState = {
  loading: false,
  loaded: false,
  probeModels: [],
  revision: 0,
  requestEpoch: 0,
  probeRequestEpoch: 0,
  loadPromise: null,
};

function syncModelQuickSelect(value) {
  $$("#modelQuickSelect [data-model-value]").forEach((button) => {
    button.classList.toggle("active", button.dataset.modelValue === value);
  });
}

function setModelSuggestions(extraModels = []) {
  const datalist = $("#modelNameOptions");
  if (!datalist) return;
  datalist.replaceChildren();
  const seen = new Set();
  const appendOption = (value, label = "") => {
    const model = String(value || "").trim();
    if (!model || seen.has(model)) return;
    seen.add(model);
    const option = document.createElement("option");
    option.value = model;
    if (label) option.label = label;
    datalist.append(option);
  };
  MODEL_QUICK_OPTIONS.forEach((item) => appendOption(item.value, item.label));
  extraModels.forEach((model) => appendOption(model));
}

function chooseModelName(model) {
  const value = String(model || "").trim();
  if (!value) return;
  const input = $("#modelName");
  if (input) {
    input.value = value;
    input.dataset.touched = "1";
    modelConfigState.revision += 1;
  }
  setModelSuggestions([value, ...modelConfigState.probeModels]);
  syncModelQuickSelect(value);
  setModelConfigMessage(`已选择模型 ${value}，记得保存`, false);
}

async function loadModelConfig({ force = false } = {}) {
  const requestedRevision = modelConfigState.revision;
  if (modelConfigState.loadPromise) {
    if (!force) return modelConfigState.loadPromise;
    modelConfigState.requestEpoch += 1;
    await modelConfigState.loadPromise;
  }
  const requestEpoch = ++modelConfigState.requestEpoch;
  modelConfigState.loading = true;
  let loadPromise;
  loadPromise = (async () => {
    try {
      const payload = await api("/api/model-config");
      if (requestEpoch !== modelConfigState.requestEpoch) return payload;
      applyModelConfig(payload, { force: force && modelConfigState.revision === requestedRevision });
      modelConfigState.loaded = true;
      return payload;
    } catch (error) {
      if (requestEpoch !== modelConfigState.requestEpoch) {
        return { status: "superseded", message: error.message };
      }
      setModelConfigMessage(`加载模型配置失败：${error.message}`, true);
      return { status: "error", message: error.message };
    } finally {
      if (modelConfigState.loadPromise === loadPromise) {
        modelConfigState.loading = false;
        modelConfigState.loadPromise = null;
      }
    }
  })();
  modelConfigState.loadPromise = loadPromise;
  return loadPromise;
}

function applyModelConfig(payload, { force = false } = {}) {
  setModelSuggestions([payload.model, ...modelConfigState.probeModels]);
  const providerSelect = $("#modelProvider");
  if (providerSelect) {
    const localProvider = providerSelect.value;
    const providerTouched = Boolean(providerSelect.dataset.touched);
    const serverFormats = Array.isArray(payload.provider_formats) && payload.provider_formats.length
      ? payload.provider_formats
      : ["deepseek", "relay"];
    const formats = providerTouched && localProvider && !serverFormats.includes(localProvider)
      ? [...serverFormats, localProvider]
      : serverFormats;
    providerSelect.innerHTML = "";
    for (const format of formats) {
      const option = document.createElement("option");
      option.value = format;
      option.textContent = format === "deepseek" ? "deepseek" : "relay (OpenAI 兼容)";
      providerSelect.append(option);
    }
    // Don't clobber an in-progress edit on tab re-entry; only set when the user
    // hasn't touched the field (or when forced, e.g. right after a save).
    if (force || !providerTouched) {
      providerSelect.value = payload.provider || formats[0];
    } else if (formats.includes(localProvider)) {
      providerSelect.value = localProvider;
    }
  }
  const nameInput = $("#modelName");
  if (nameInput && (force || !nameInput.dataset.touched)) nameInput.value = payload.model || "";
  syncModelQuickSelect(nameInput?.value || "");
  const baseInput = $("#modelBaseUrl");
  if (baseInput && (force || !baseInput.dataset.touched)) baseInput.value = payload.base_url || "";
  const waitInput = $("#modelMaxWait");
  if (waitInput && (force || !waitInput.dataset.touched)) {
    waitInput.value = payload.max_wait_seconds != null ? payload.max_wait_seconds : "";
  }
  const concurrencyInput = $("#modelMaxConcurrency");
  if (concurrencyInput && (force || !concurrencyInput.dataset.touched)) {
    concurrencyInput.value = payload.max_concurrency != null ? payload.max_concurrency : (payload.recommended_max_concurrency || "");
  }
  if (force) clearModelConfigTouched();
}

function clearModelConfigTouched() {
  for (const sel of ["#modelProvider", "#modelName", "#modelBaseUrl", "#modelMaxWait", "#modelMaxConcurrency"]) {
    const el = $(sel);
    if (el) delete el.dataset.touched;
  }
}

function modelConfigPayload() {
  const maxWaitRaw = String($("#modelMaxWait")?.value || "").trim();
  const maxConcurrencyRaw = String($("#modelMaxConcurrency")?.value || "").trim();
  return {
    provider: $("#modelProvider")?.value || "",
    model: String($("#modelName")?.value || "").trim(),
    base_url: String($("#modelBaseUrl")?.value || "").trim(),
    ...(maxWaitRaw ? { max_wait_seconds: Number(maxWaitRaw) } : {}),
    ...(maxConcurrencyRaw ? { max_concurrency: Number(maxConcurrencyRaw) } : {}),
  };
}

async function saveModelConfig(helpers = {}) {
  const payload = modelConfigPayload();
  const savedRevision = modelConfigState.revision;
  if (!payload.model) {
    setModelConfigMessage("请填写模型名", true);
    return { status: "error" };
  }
  if (!payload.base_url) {
    setModelConfigMessage("请填写请求地址 base_url", true);
    return { status: "error" };
  }
  helpers.update?.(30, "正在保存模型配置");
  // Any GET already in flight describes state from before this save. Its
  // response may still complete, but it must not be applied afterward.
  modelConfigState.requestEpoch += 1;
  try {
    const result = await api("/api/model-config", { method: "POST", body: JSON.stringify(payload) });
    // A GET may have started while the POST was pending and can still contain
    // the pre-save snapshot. Advance again before applying the committed value.
    modelConfigState.requestEpoch += 1;
    const unchanged = modelConfigState.revision === savedRevision;
    applyModelConfig(result.model_config || {}, { force: unchanged });
    setModelConfigMessage(unchanged ? "模型配置已保存" : "模型配置已保存；还有新的修改待保存", false);
    setStatusMessage(unchanged ? "模型配置已保存" : "模型配置已保存；还有新的修改待保存");
    return result;
  } catch (error) {
    setModelConfigMessage(`保存失败：${error.message}`, true);
    return { status: "error", message: error.message };
  }
}

async function probeModelFetch(helpers = {}) {
  const payload = modelConfigPayload();
  if (!payload.base_url) {
    setModelConfigMessage("请先填写请求地址 base_url", true);
    return { status: "error" };
  }
  const submittedRevision = modelConfigState.revision;
  const submittedFingerprint = JSON.stringify(payload);
  const requestEpoch = ++modelConfigState.probeRequestEpoch;
  const box = $("#modelProbeResult");
  if (box) {
    box.hidden = true;
    box.textContent = "";
  }
  helpers.update?.(30, "正在试拉取模型列表");
  try {
    const result = await api("/api/model-config/probe", { method: "POST", body: JSON.stringify(payload) });
    if (
      requestEpoch !== modelConfigState.probeRequestEpoch
      || submittedRevision !== modelConfigState.revision
      || submittedFingerprint !== JSON.stringify(modelConfigPayload())
    ) {
      const ignored = { ...result, ignored: true, ignore_reason: "stale_model_probe_response" };
      setModelConfigMessage("模型配置已变化，旧探测结果已忽略", false);
      return ignored;
    }
    renderModelProbe(result);
    if (result.status === "ok") {
      setModelConfigMessage(`试拉取成功，返回 ${result.model_count} 个模型`, false);
    } else {
      setModelConfigMessage(`试拉取失败：${result.error || "unknown"}`, true);
    }
    return result;
  } catch (error) {
    if (
      requestEpoch !== modelConfigState.probeRequestEpoch
      || submittedRevision !== modelConfigState.revision
      || submittedFingerprint !== JSON.stringify(modelConfigPayload())
    ) {
      return { status: "superseded", ignored: true, ignore_reason: "stale_model_probe_response" };
    }
    renderModelProbe({ status: "error", error: error.message });
    setModelConfigMessage(`试拉取失败：${error.message}`, true);
    return { status: "error", message: error.message };
  }
}

function renderModelProbe(result) {
  const box = $("#modelProbeResult");
  if (!box) return;
  box.hidden = false;
  box.classList.toggle("error", result.status !== "ok");
  if (result.status !== "ok") {
    const detail = result.http_status ? `HTTP ${result.http_status}` : (result.error || "unknown");
    box.textContent = `不可达 / ${detail}`;
    return;
  }
  const models = Array.isArray(result.models) ? result.models : [];
  modelConfigState.probeModels = models;
  setModelSuggestions([result.configured_model, ...models]);
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "model-probe-head";
  const configured = result.configured_model;
  const availability = result.configured_model_available;
  head.textContent = `可达 · ${result.model_count} 个模型` +
    (configured ? ` · 当前模型 ${configured} ${availability ? "✓ 可用" : "✗ 不在列表"}` : "");
  box.append(head);
  const list = document.createElement("div");
  list.className = "model-probe-list";
  for (const model of models.slice(0, 40)) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "model-chip" + (model === configured ? " active" : "");
    chip.textContent = model;
    chip.addEventListener("click", () => {
      chooseModelName(model);
    });
    list.append(chip);
  }
  box.append(list);
}

function setModelConfigMessage(message, isError) {
  const node = $("#modelConfigMessage");
  if (!node) return;
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", Boolean(isError));
  if (message && !isError) {
    setTimeout(() => {
      if (node.textContent === message) node.hidden = true;
    }, 3000);
  }
}

async function loadKeyPool({ force = false } = {}) {
  if (keyPoolState.loadPromise) {
    if (!force) return keyPoolState.loadPromise;
    keyPoolState.requestEpoch += 1;
    await keyPoolState.loadPromise;
  }
  const requestEpoch = ++keyPoolState.requestEpoch;
  keyPoolState.loading = true;
  let loadPromise;
  loadPromise = (async () => {
    try {
      const payload = await api("/api/keys");
      if (requestEpoch !== keyPoolState.requestEpoch) return payload;
      applyKeyPoolPayload(payload);
      return payload;
    } catch (error) {
      if (requestEpoch !== keyPoolState.requestEpoch) {
        return { status: "superseded", message: error.message };
      }
      setKeyPoolMessage(`加载密钥池失败：${error.message}`, true);
      return { status: "error", message: error.message };
    } finally {
      if (keyPoolState.loadPromise === loadPromise) {
        keyPoolState.loading = false;
        keyPoolState.loadPromise = null;
      }
    }
  })();
  keyPoolState.loadPromise = loadPromise;
  return loadPromise;
}

async function refreshModelConfiguration(helpers = {}) {
  helpers.update?.(25, "正在读取模型配置");
  // An explicit refresh intentionally discards local model edits.
  clearModelConfigTouched();
  modelConfigState.revision += 1;
  const modelResult = await loadModelConfig({ force: true });
  helpers.update?.(65, "正在读取密钥池");
  const keyPoolResult = await loadKeyPool({ force: true });
  const failures = [modelResult, keyPoolResult]
    .filter((result) => result?.status === "error")
    .map((result) => String(result.message || "unknown_error"));
  if (failures.length) {
    const message = `刷新模型配置失败：${failures.join("；")}`;
    setModelConfigMessage(message, true);
    setStatusMessage(message);
    return {
      status: "error",
      message,
      model_config: modelResult,
      key_pool: keyPoolResult,
    };
  }
  return { status: "ok", model_config: modelResult, key_pool: keyPoolResult };
}

function applyKeyPoolPayload(payload) {
  keyPoolState.keys = Array.isArray(payload.keys) ? payload.keys : [];
  keyPoolState.keyFile = payload.key_file || "";
  keyPoolState.writable = Boolean(payload.key_file_writable);
  renderKeyPool();
}

function renderKeyPool() {
  const summary = $("#keyPoolSummary");
  const list = $("#keyPoolList");
  if (!summary || !list) return;
  const total = keyPoolState.keys.length;
  const available = keyPoolState.keys.filter((item) => item.available).length;
  summary.textContent = total
    ? `共 ${total} 个密钥，${available} 个可用`
    : "尚未配置密钥";
  list.innerHTML = "";
  keyPoolState.keys.forEach((item) => list.appendChild(renderKeyRow(item)));
  const addButton = $("#addKey");
  const input = $("#newKeyValue");
  if (addButton) addButton.disabled = !keyPoolState.writable;
  if (input) input.disabled = !keyPoolState.writable;
}

function renderKeyRow(item) {
  const row = document.createElement("div");
  row.className = "key-pool-row";
  const info = document.createElement("div");
  info.className = "key-pool-info";
  const preview = document.createElement("span");
  preview.className = "key-pool-preview";
  preview.textContent = item.preview || (item.available ? "已配置" : "未设置");
  const meta = document.createElement("span");
  meta.className = `key-pool-meta ${item.available ? "ok" : "warn"}`;
  const sourceLabel = { file_secret: "文件密钥", file_env: "文件环境变量", env: "环境变量" }[item.source] || item.source;
  meta.textContent = `${sourceLabel} · ${item.available ? "可用" : "不可用"}`;
  const model = item.model_config || {};
  const modelMeta = document.createElement("span");
  modelMeta.className = "key-pool-meta";
  const concurrency = model.max_concurrency ? ` · 并发 ${model.max_concurrency}` : "";
  modelMeta.textContent = `${model.provider || "default"} · ${model.model || "未配置模型"} · ${model.base_url || "未配置 base_url"}${concurrency}`;
  info.appendChild(preview);
  info.appendChild(meta);
  info.appendChild(modelMeta);
  row.appendChild(info);
  if (item.source === "file_secret") {
    row.appendChild(
      actionButton("移除", "ghost small danger", (helpers) => removeKey(item.ref, helpers), {
        label: "移除 API 密钥",
        category: "密钥池",
        scope: "settings:key-pool",
      }),
    );
  } else {
    const note = document.createElement("span");
    note.className = "key-pool-note";
    note.textContent = "在配置中管理";
    row.appendChild(note);
  }
  return row;
}

async function addKey(helpers = {}) {
  const input = $("#newKeyValue");
  const value = (input?.value || "").trim();
  const submittedRevision = keyPoolState.inputRevision;
  if (!value) {
    setKeyPoolMessage("请先粘贴密钥", true);
    return { status: "error" };
  }
  helpers.update?.(30, "正在写入密钥池");
  keyPoolState.requestEpoch += 1;
  try {
    const payload = await api("/api/keys/add", { method: "POST", body: JSON.stringify({ value }) });
    keyPoolState.requestEpoch += 1;
    if (
      input
      && keyPoolState.inputRevision === submittedRevision
      && String(input.value || "").trim() === value
    ) {
      input.value = "";
    }
    applyKeyPoolPayload(payload);
    setKeyPoolMessage("已添加密钥", false);
    setStatusMessage("密钥池已更新");
    return payload;
  } catch (error) {
    setKeyPoolMessage(`添加失败：${error.message}`, true);
    return { status: "error", message: error.message };
  }
}

async function removeKey(ref, helpers = {}) {
  if (!window.confirm("确定移除这个 API 密钥吗？\n\n依赖该密钥的模型请求可能立即不可用。")) {
    return { status: "cancelled_by_user", message: "用户取消移除 API 密钥" };
  }
  helpers.update?.(30, "正在移除密钥");
  keyPoolState.requestEpoch += 1;
  try {
    const payload = await api("/api/keys/remove", { method: "POST", body: JSON.stringify({ ref }) });
    keyPoolState.requestEpoch += 1;
    applyKeyPoolPayload(payload);
    setKeyPoolMessage("已移除密钥", false);
    setStatusMessage("密钥池已更新");
    return payload;
  } catch (error) {
    setKeyPoolMessage(`移除失败：${error.message}`, true);
    return { status: "error", message: error.message };
  }
}

function setKeyPoolMessage(message, isError) {
  const node = $("#keyPoolMessage");
  if (!node) return;
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", Boolean(isError));
  if (message) {
    setTimeout(() => {
      if (node.textContent === message) node.hidden = true;
    }, 3000);
  }
}

document.addEventListener("click", (event) => {
  const pageButton = event.target.closest(".page-tabs button");
  if (pageButton) {
    const page = pageButton.dataset.page;
    if (page && state.activePage !== page) {
      setActivePage(page);
    }
    return;
  }
  const metric = event.target.closest(".metric");
  if (metric) {
    setActiveStatus(metric.dataset.status);
    return;
  }
  const modeButton = event.target.closest("#sendModeSegment button");
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

$("#sendEnabled")?.addEventListener("change", markControlsDirty);
$("#driverSelect")?.addEventListener("change", markControlsDirty);
$("#sendBackendSelect")?.addEventListener("change", markControlsDirty);
$("#ocrModeSelect")?.addEventListener("change", markControlsDirty);
$("#asrModeSelect")?.addEventListener("change", markControlsDirty);
$("#fileMaxMb")?.addEventListener("input", markControlsDirty);
bindTaskButton("#refreshButton", {
  label: "刷新运行状态",
  category: "系统",
  scope: "ui:refresh",
}, (helpers) => {
  helpers.update(30, "正在读取 /api/state");
  return refresh({ forceControls: !state.controlsDirty, force: true });
});
bindTaskButton("#saveControls", {
  label: "保存发送控制",
  category: "发送控制",
  scope: "settings:send-controls",
}, (helpers) => {
  helpers.update(25, "正在保存发送控制");
  return saveControls();
});
bindTaskButton("#driverProbeButton", {
  label: "探测发送后端",
  category: "发送控制",
  scope: "diagnostic:driver-probe",
}, (helpers) => {
  helpers.update(25, "正在按已保存配置探测发送后端");
  return probeDriverNow();
});
bindTaskButton("#probeButton", {
  label: "探测微信窗口",
  category: "诊断",
  scope: "diagnostic:wechat-probe",
}, (helpers) => {
  helpers.update(28, "正在探测微信窗口句柄");
  return probeNow();
});
bindTaskButton("#runtimeProbeButton", {
  label: "审查 OCR/ASR 运行路径",
  category: "环境",
  scope: "diagnostic:runtime-gpu",
}, (helpers) => probeRuntimeGpu(helpers));
bindTaskButton("#resourceAuditButton", {
  label: "本机资源审计",
  category: "资源",
  scope: "diagnostic:resource-audit",
}, (helpers) => auditLocalResources(helpers));
bindTaskButton("#diagnosticsExportButton", {
  label: "导出诊断包",
  category: "诊断",
  scope: "diagnostic:export",
}, (helpers) => exportDiagnosticsBundle(helpers));
bindTaskButton("#agentTickButton", {
  label: "运行对话 Agent",
  category: "Agent",
  scope: "agent:tick",
}, (helpers) => runAgentTick(helpers));
bindTaskButton("#agentStartButton", {
  label: "启动连续对话 Agent",
  category: "Agent",
  scope: "agent:worker",
}, (helpers) => runAgentWorkerAction("start", helpers));
bindTaskButton("#agentStopButton", {
  label: "停止连续对话 Agent",
  category: "Agent",
  scope: "agent:worker",
}, (helpers) => runAgentWorkerAction("stop", helpers));
bindTaskButton("#bridgeRefreshButton", {
  label: "刷新非前台桥",
  category: "非前台桥",
  scope: "ui:bridge-refresh",
}, () => refresh({ force: true }));
bindTaskButton("#runtimeRefreshButton", {
  label: "刷新技能/人设",
  category: "技能/人设",
  scope: "ui:runtime-refresh",
}, () => refresh({ force: true }));
bindTaskButton("#clearAuditButton", {
  label: "清空发送审计",
  category: "发送审计",
  scope: "audit:clear",
}, (helpers) => clearSendAudit(helpers));
bindTaskButton("#storageStatusButton", {
  label: "审查存储与数据库合同",
  category: "历史数据",
  scope: "history:storage-status",
}, (helpers) => inspectStorageStatus(helpers));
bindTaskButton("#clearHistoryDataButton", {
  label: "清空历史数据",
  category: "历史数据",
  scope: "history:clear",
}, (helpers) => clearHistoryData(helpers));
bindTaskButton("#weflowRefreshButton", {
  label: "刷新 WeFlow 状态",
  category: "WeFlow",
  scope: "weflow:info",
}, (helpers) => {
  helpers.update(24, "正在刷新 WeFlow 状态");
  state.weflowStatusMode = "live";
  if (state.weflowLatestStatusText) showWeFlowStatusText(state.weflowLatestStatusText, "live");
  return refresh({ force: true });
});
bindTaskButton("#weflowHealthButton", {
  label: "WeFlow Health 检查",
  category: "WeFlow",
  scope: "weflow:info",
}, (helpers) => weflowAction("health", {}, helpers));
bindTaskButton("#weflowDiscoverButton", {
  label: "发现 WeFlow 会话",
  category: "WeFlow",
  scope: "weflow:info",
}, (helpers) => weflowDiscoverSessions(helpers));
bindTaskButton("#weflowClearHistoryButton", {
  label: "清空 WeFlow 操作历史",
  category: "WeFlow",
  scope: "weflow:history",
}, (helpers) => clearWeFlowHistory(helpers));
bindTaskButton("#weflowPullButton", {
  label: "WeFlow 拉取一次",
  category: "WeFlow",
  scope: "weflow:exclusive",
}, (helpers) => weflowAction("pull-once", { background: true }, helpers));
bindTaskButton("#weflowBackfillButton", {
  label: "WeFlow 回填历史",
  category: "WeFlow",
  scope: "weflow:exclusive",
}, (helpers) => weflowBackfill(helpers));
bindTaskButton("#weflowCancelBackfillButton", {
  label: "取消 WeFlow 回填",
  category: "WeFlow",
  scope: "weflow:control",
}, (helpers) => weflowCancelBackfill(helpers));
bindTaskButton("#weflowStartButton", {
  label: "启动 WeFlow 后台",
  category: "WeFlow",
  scope: "weflow:worker",
}, (helpers) => weflowAction("start", {}, helpers));
bindTaskButton("#weflowStopButton", {
  label: "停止 WeFlow 后台",
  category: "WeFlow",
  scope: "weflow:worker",
}, (helpers) => weflowAction("stop", {}, helpers));
bindTaskButton("#weflowDepsButton", {
  label: "检查 WeFlow 依赖",
  category: "WeFlow",
  scope: "weflow:exclusive",
}, (helpers) => weflowDependencies(helpers));
bindTaskButton("#weflowInstallButton", {
  label: "安装 WeFlow 依赖",
  category: "WeFlow",
  scope: "weflow:exclusive",
}, (helpers) => weflowInstallDeps(helpers));
bindTaskButton("#nativeMigrationProbeButton", {
  label: "微信 Native 迁移探测",
  category: "WeFlow",
  scope: "diagnostic:native-migration",
}, (helpers) => nativeMigrationProbe(helpers));
$("#weflowHelpButton")?.addEventListener("click", () => {
  setWeFlowHelpVisible($("#weflowHelpPopover")?.hidden !== false);
});
$("#weflowHelpCloseButton")?.addEventListener("click", () => setWeFlowHelpVisible(false));
["#weflowBaseUrl", "#weflowTokenEnv"].forEach((selector) => {
  $(selector).addEventListener("input", (event) => {
    event.target.dataset.touched = "1";
  });
});
$("#weflowTalkers").addEventListener("input", renderTalkerChips);
$("#weflowStoredSessionFilter").addEventListener("input", () =>
  renderWeFlowStoredSessions(state.data?.weflow?.discovered_sessions?.sessions || []),
);
$("#personaForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask(
    {
      label: "保存并装备人物卡",
      category: "技能/人设",
      scope: "settings:runtime-cards",
      button: event.submitter || null,
    },
    () => savePersonaCard(event),
  );
});
PERSONA_CARD_FIELDS.forEach((selector) => {
  $(selector)?.addEventListener("input", () => {
    state.personaCardRevision += 1;
  });
});
$("#channelRuntimeForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask(
    {
      label: "保存通道人设覆盖",
      category: "技能/人设",
      scope: "settings:runtime-cards",
      button: event.submitter || null,
    },
    () => saveChannelRuntimeOverride(event),
  );
});
bindTaskButton("#clearChannelPersonaButton", {
  label: "清除通道人物卡覆盖",
  category: "技能/人设",
  scope: "settings:runtime-cards",
}, clearChannelPersonaOverride);
bindTaskButton("#clearChannelSkillsButton", {
  label: "清除通道技能覆盖",
  category: "技能/人设",
  scope: "settings:runtime-cards",
}, clearChannelSkillsOverride);
$("#taskForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask(
    {
      label: "保存并装备任务卡",
      category: "技能/人设",
      scope: "settings:runtime-cards",
      button: event.submitter || null,
    },
    () => saveTaskCard(event),
  );
});
TASK_CARD_FIELDS.forEach((selector) => {
  $(selector)?.addEventListener("input", () => {
    state.taskCardRevision += 1;
  });
});
$("#toggleProbe").addEventListener("click", () => {
  state.probeExpanded = !state.probeExpanded;
  renderProbeJson();
});
bindTaskButton("#addKey", {
  label: "添加 API 密钥",
  category: "密钥池",
  scope: "settings:key-pool",
}, (helpers) => addKey(helpers));
$("#newKeyValue").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    $("#addKey").click();
  }
});
$("#newKeyValue").addEventListener("input", () => {
  keyPoolState.inputRevision += 1;
});
bindTaskButton("#refreshModelConfig", {
  label: "刷新模型配置",
  category: "模型配置",
  scope: "settings:model-config",
}, (helpers) => refreshModelConfiguration(helpers));
["#modelProvider", "#modelName", "#modelBaseUrl", "#modelMaxWait", "#modelMaxConcurrency"].forEach((selector) => {
  const el = $(selector);
  if (el) {
    const evt = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(evt, () => {
      el.dataset.touched = "1";
      modelConfigState.revision += 1;
      if (selector === "#modelName") syncModelQuickSelect(el.value);
    });
  }
});
$$("[data-model-value]").forEach((button) => {
  button.addEventListener("click", () => chooseModelName(button.dataset.modelValue || button.textContent));
});
bindTaskButton("#probeModelButton", {
  label: "试拉取模型",
  category: "模型配置",
  scope: "settings:model-config",
}, (helpers) => probeModelFetch(helpers));
$("#modelConfigForm").addEventListener("submit", (event) => {
  event.preventDefault();
  runTask(
    {
      label: "保存模型配置",
      category: "模型配置",
      scope: "settings:model-config",
      button: event.submitter || null,
    },
    (helpers) => saveModelConfig(helpers),
  );
});

refresh({ forceControls: true });
setActivePage("overview");
setActivePanel("queue");
initBoundedTooltips();
window.addEventListener("scroll", repositionTaskProgressPopovers, true);
window.addEventListener("resize", repositionTaskProgressPopovers);
renderTaskQueue();
setInterval(() => {
  if (!state.controlsSaving) refresh();
}, 1800);
