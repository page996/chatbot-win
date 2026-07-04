const state = {
  data: null,
  activeStatus: "pending",
  refreshing: false,
  controlsDirty: false,
  controlsSaving: false,
  statusMessage: "",
  probeExpanded: false,
  activePage: "overview",
  activePanel: "queue",
  weflowStatusMode: "live",
  weflowLatestStatusText: "",
  taskSeq: 0,
  tasks: [],
  taskHistory: [],
  taskScopeChains: new Map(),
  backfillTaskByJobId: new Map(),
  taskPopovers: new Map(),
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
    const error = new Error(payload.error || payload.message || `HTTP ${response.status}`);
    error.payload = payload;
    error.httpStatus = response.status;
    throw error;
  }
  return payload;
}

async function refresh({ forceControls = false, force = false } = {}) {
  if (state.refreshing || (state.controlsSaving && !force)) return;
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
  renderTaskQueue();
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
  return names.length ? names : ["not_implemented", "bridge_outbox"];
}

function renderWechatProbe(probe) {
  const active = probe.active || {};
  const windows = probe.windows || [];
  const first = windows[0] || {};
  $("#diagnosticDetail").textContent = [
    active.title || first.title || "未发现可用微信聊天窗口",
    probeStatusText(active.status || probe.status || "unknown"),
    first.process_name || "",
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
    actions.append(actionButton("清除通道", "ghost small", () => deleteChannel(channel.conversation_id), {
      label: `清除通道：${displayName}`,
      category: "通道",
      scope: `conversation:${channel.conversation_id}`,
      target: channel.conversation_id,
    }));
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
      <div class="conversation">${escapeHtml(conversationLabel(reply.conversation_id || ""))}</div>
      <div class="reply-text">${escapeHtml(reply.text || "")}</div>
      <div class="actions"></div>
    `;
    const actions = node.querySelector(".actions");
    const conversationId = reply.conversation_id || item.conversation_id || "";
    if (item.status === "pending") {
      actions.append(actionButton("通过", "primary", () => queueAction(item.queue_id, "approve"), {
        label: `通过回复：${conversationId || item.queue_id}`,
        category: "发送审核",
        scope: `conversation:${conversationId || item.queue_id}`,
        target: conversationId,
      }));
      actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject"), {
        label: `拒绝回复：${conversationId || item.queue_id}`,
        category: "发送审核",
        scope: `conversation:${conversationId || item.queue_id}`,
        target: conversationId,
      }));
    }
    if (item.status === "approved") {
      actions.append(actionButton("3秒后发送", "primary", () => delayedQueueAction(item.queue_id, "send-approved"), {
        label: `发送回复：${conversationId || item.queue_id}`,
        category: "发送审核",
        scope: `conversation:${conversationId || item.queue_id}`,
        target: conversationId,
      }));
      actions.append(actionButton("拒绝", "danger", () => queueAction(item.queue_id, "reject"), {
        label: `拒绝回复：${conversationId || item.queue_id}`,
        category: "发送审核",
        scope: `conversation:${conversationId || item.queue_id}`,
        target: conversationId,
      }));
    }
    if (conversationId && channelByConversationId(conversationId)) {
      actions.append(actionButton("×", "danger mini", () => deleteChannel(conversationId), {
        label: `删除服务通道：${conversationLabel(conversationId)}`,
        category: "通道",
        scope: `conversation:${conversationId}`,
        target: conversationId,
      }));
    }
    list.append(node);
  }
}

function renderBridge(bridge) {
  $("#bridgePendingCount").textContent = `${bridge.pending_count || 0} 待消费 / ${bridge.ack_count || 0} 已确认 / ${bridge.channel_count || 0} 通道`;
  $("#bridgePath").textContent = bridge.outbox_path || "未创建 outbox";
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
          scope: `conversation:${conversationId}`,
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
  for (const item of items.slice(-10).reverse()) {
    const node = document.createElement("article");
    node.className = `bridge-item status-${item.status || "queued"}`;
    node.innerHTML = `
      <div class="queue-head">
        <span>${escapeHtml(bridgeStatusText(item.status || "queued"))}</span>
        <time>${escapeHtml(shortTime(item.created_at || ""))}</time>
      </div>
      <div class="conversation">${escapeHtml(conversationLabel(item.conversation_id || ""))}</div>
      <div class="reply-text">${escapeHtml(item.text || "")}</div>
      <p>${escapeHtml([item.bridge_id || "", item.receiver ? `receiver=${item.receiver}` : ""].filter(Boolean).join(" / "))}</p>
      <div class="actions"></div>
    `;
    const actions = node.querySelector(".actions");
    if ((item.status || "queued") === "queued") {
      actions.append(actionButton("标记已发", "primary", () => ackBridge(item.bridge_id, "sent"), {
        label: `桥接标记已发：${item.conversation_id || item.bridge_id}`,
        category: "非前台桥",
        scope: `conversation:${item.conversation_id || item.bridge_id}`,
        target: item.conversation_id || "",
      }));
      actions.append(actionButton("标记失败", "danger", () => ackBridge(item.bridge_id, "failed"), {
        label: `桥接标记失败：${item.conversation_id || item.bridge_id}`,
        category: "非前台桥",
        scope: `conversation:${item.conversation_id || item.bridge_id}`,
        target: item.conversation_id || "",
      }));
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

function renderWeFlow(weflow) {
  const worker = weflow.worker || {};
  const backfillJob = weflow.backfill_job || {};
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
    worker.stop_requested ? "正在停止后台拉取" : "",
    metrics.slow_ticks ? `慢 tick ${metrics.slow_ticks} 次` : "",
    bridgeState.session_count ? `会话游标 ${bridgeState.session_count} / 去重 ${bridgeState.seen_raw_id_count || 0}` : "",
    worker.last_error ? `后台错误：${worker.last_error}` : "",
  ].filter(Boolean).join(" / ");
  if (!$("#weflowBaseUrl").dataset.touched) $("#weflowBaseUrl").value = weflow.base_url || "http://127.0.0.1:5031";
  if (!$("#weflowTokenEnv").dataset.touched) $("#weflowTokenEnv").value = weflow.token_env || "WEFLOW_API_TOKEN";
  const statusPayload = {
    worker,
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
    config_migration: weflow.config_migration || {},
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
  const startButton = $("#weflowStartButton");
  if (startButton && !startButton.dataset.taskLocked) {
    startButton.disabled = Boolean(worker.running);
  }
  const stopButton = $("#weflowStopButton");
  if (stopButton && !stopButton.dataset.taskLocked) {
    stopButton.disabled = !Boolean(worker.running || worker.stop_requested);
  }
  const envLocked = Boolean(worker.running || backfillJob.running || backfillJob.status === "cancel_requested");
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
    const node = document.createElement("article");
    node.className = "stored-session-item";
    node.innerHTML = `
      <div>
        <strong>${escapeHtml(session.name || sessionId)}</strong>
        <span>${escapeHtml(conversationTypeText(session.type || ""))} / ${escapeHtml(session.cached ? "本地库" : "实时发现")}</span>
      </div>
      <div class="stored-session-actions"></div>
    `;
    const actions = node.querySelector(".stored-session-actions");
    actions.append(simpleButton(selected.has(sessionId) ? "已加入" : "加入", "ghost mini", () => addTalker(sessionId)));
    if (session.conversation_id) {
      actions.append(actionButton("×", "danger mini", () => deleteChannel(session.conversation_id), {
        label: `删除本地通道：${session.name || sessionId}`,
        category: "通道",
        scope: `conversation:${session.conversation_id}`,
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

async function clearSendAudit(helpers = {}) {
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

function runTask(meta, worker) {
  const task = createTask(meta);
  const scope = task.scope || "global";
  const previous = state.taskScopeChains.get(scope) || Promise.resolve();
  const runner = previous
    .catch(() => null)
    .then(() => executeTask(task, worker));
  const cleanup = runner.finally(() => {
    if (state.taskScopeChains.get(scope) === cleanup) {
      state.taskScopeChains.delete(scope);
    }
  });
  state.taskScopeChains.set(scope, cleanup);
  return cleanup;
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
    queuedAt: now,
    startedAt: "",
    finishedAt: "",
    detail: meta.detail || "",
    button: meta.button || null,
  };
  if (task.button) {
    task.button.disabled = true;
    task.button.dataset.taskLocked = task.id;
    createButtonTaskProgress(task);
  }
  state.tasks.unshift(task);
  state.tasks = state.tasks.slice(0, 60);
  recordTaskHistory(task, "created", "任务已加入队列");
  renderTaskQueue();
  return task;
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
  setTimeout(() => removeButtonTaskProgress(task.id), 1600);
}

function updateTask(taskId, patch) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) return;
  if (patch.progress !== undefined) {
    patch.progress = clampPercent(patch.progress);
  }
  Object.assign(task, patch);
  renderTaskQueue();
  updateButtonTaskProgress(task);
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
  status.textContent = `${taskStatusText(task.status)} ${clampPercent(task.progress)}%`;
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
  const top = buttonRect.bottom + gap;
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
  const countNode = $("#taskActiveCount");
  const list = $("#taskQueueList");
  if (!countNode || !list) return;
  const activeCount = state.tasks.filter((task) => ["queued", "running"].includes(task.status)).length;
  countNode.textContent = `${activeCount} 进行中`;
  list.innerHTML = "";
  if (!state.tasks.length) {
    list.append(emptyNode("还没有任务记录"));
    return;
  }
  for (const task of orderedTasks().slice(0, 24)) {
    const node = document.createElement("article");
    node.className = `task-item status-${task.status}`;
    node.innerHTML = `
      <div class="task-head">
        <strong>${escapeHtml(task.label)}</strong>
        <span class="task-status">${escapeHtml(taskStatusText(task.status))}</span>
      </div>
      <div class="task-meta">
        <span>${escapeHtml(task.category)} / ${escapeHtml(task.scopeLabel)}</span>
        <time>${escapeHtml(taskTimeRange(task))}</time>
      </div>
      <div class="progress-track"><span style="width: ${clampPercent(task.progress)}%"></span></div>
      <div class="task-progress-label">${clampPercent(task.progress)}%</div>
      <div class="task-phase">${escapeHtml(task.phase || task.detail || "")}</div>
    `;
    list.append(node);
  }
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
    node.innerHTML = `
      <div class="history-entry-time">${escapeHtml(formatLocalTime(entry.time))}</div>
      <div class="history-entry-action">${escapeHtml(entry.category)} / ${escapeHtml(taskEventText(entry.event))} / ${escapeHtml(entry.label)}</div>
      <div class="progress-track"><span style="width: ${clampPercent(entry.progress)}%"></span></div>
      <div class="history-entry-result">${escapeHtml(entry.detail || entry.scopeLabel || "")}</div>
    `;
    list.append(node);
  }
}

function orderedTasks() {
  const priority = { running: 0, queued: 1, failed: 2, cancelled: 3, completed: 4 };
  return [...state.tasks].sort((left, right) => {
    const byStatus = (priority[left.status] ?? 9) - (priority[right.status] ?? 9);
    if (byStatus) return byStatus;
    return String(right.queuedAt).localeCompare(String(left.queuedAt));
  });
}

function taskResultFailed(result) {
  if (!result || typeof result !== "object") return false;
  return ["error", "failed", "partial_error"].includes(String(result.status || ""));
}

function taskStatusText(status) {
  return {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status || "";
}

function taskEventText(event) {
  return {
    created: "创建",
    started: "开始",
    finished: "结束",
    failed: "失败",
    cancelled: "取消",
  }[event] || event || "";
}

function taskScopeText(scope) {
  const value = String(scope || "");
  if (value.startsWith("conversation:")) return "同会话串行";
  if (value.startsWith("weflow:exclusive")) return "WeFlow 独占队列";
  if (value.startsWith("weflow:pull")) return "WeFlow 拉取串行";
  if (value.startsWith("weflow:")) return "WeFlow 独立队列";
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

function clampPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, Math.round(number)));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function saveControls() {
  if (state.controlsSaving) return;
  state.controlsSaving = true;
  setDirtyIndicator("saving");
  try {
    const mode = currentMode();
    await api("/api/controls", {
      method: "POST",
      body: JSON.stringify({
        mode,
        send_enabled: $("#sendEnabled").checked,
        send_driver: $("#driverSelect").value,
        send_confirm_required: mode !== "auto",
      }),
    });
    state.controlsDirty = false;
    setStatusMessage("发送控制已保存");
    await refresh({ forceControls: true, force: true });
    return { status: "ok" };
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
  return queueAction(queueId, action);
}

async function deleteChannel(conversationId) {
  if (!conversationId) return;
  const payload = await api(`/api/channels/delete/${encodeURIComponent(conversationId)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  setStatusMessage(payload.note || "通道已清除");
  await refresh({ force: true });
  return payload;
}

async function cleanupHiddenChannels() {
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
  const payload = await api("/api/bridge/ack", {
    method: "POST",
    body: JSON.stringify({
      bridge_id: bridgeId,
      status,
      reason: status === "sent" ? "manual_sidebar_ack" : "manual_sidebar_failed",
    }),
  });
  setStatusMessage(status === "sent" ? "桥接项已标记为已发" : "桥接项已标记为失败");
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

async function savePersonaCard(event) {
  event.preventDefault();
  const name = $("#personaCardName").value.trim();
  const content = $("#personaCardContent").value.trim();
  if (!content) {
    setStatusMessage("人物卡内容不能为空");
    return { status: "error", message: "人物卡内容不能为空" };
  }
  const result = await runtimeCardAction("save-persona", { name, content });
  $("#personaCardName").value = "";
  $("#personaCardContent").value = "";
  return result;
}

async function saveTaskCard(event) {
  event.preventDefault();
  const name = $("#taskCardName").value.trim();
  const content = $("#taskCardContent").value.trim();
  if (!content) {
    setStatusMessage("任务卡内容不能为空");
    return { status: "error", message: "任务卡内容不能为空" };
  }
  const result = await runtimeCardAction("save-task", { name, content });
  $("#taskCardName").value = "";
  $("#taskCardContent").value = "";
  return result;
}

async function probeNow() {
  const payload = await api("/api/wechat-probe");
  state.data = { ...(state.data || {}), wechat_window_probe: payload };
  renderWechatProbe(payload);
  renderProbeJson();
  return payload;
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
    <div class="session-item" data-session-id="${escapeHtml(s.id || "")}">
      <div>
        <div class="session-item-name">${escapeHtml(s.name || s.id || "（无名称）")}</div>
        <div class="session-item-id">${escapeHtml(s.id || "")}</div>
      </div>
    </div>
  `
    )
    .join("");
  list.querySelectorAll(".session-item").forEach((item) => {
    item.addEventListener("click", () => {
      const sessionId = item.dataset.sessionId;
      if (sessionId) {
        addTalker(sessionId);
        list.hidden = true;
      }
    });
  });
}

function showWeFlowStatusText(text, mode = "action") {
  state.weflowStatusMode = mode;
  $("#weflowStatusBox").textContent = text;
}

function showWeFlowStatusPayload(payload, mode = "action") {
  showWeFlowStatusText(JSON.stringify(compactPayload(payload, 5000), null, 2), mode);
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
  if (source.scanned_count !== undefined) parts.push(`源扫描=${source.scanned_count}`);
  if (source.appended_count !== undefined) parts.push(`源新增=${source.appended_count}`);
  if (imported.appended_count !== undefined) parts.push(`导入后端=${imported.appended_count}`);
  if (pull.processed_count !== undefined) parts.push(`写入对话=${pull.processed_count}`);
  if (result.message || result.error) parts.push(String(result.message || result.error));
  const legacy = normalizeLegacyWeFlowSummary(entry?.summary);
  return parts.length ? parts.join(" / ") : (legacy || JSON.stringify(compactPayload(result || entry, 300)));
}

function normalizeLegacyWeFlowSummary(summary) {
  const text = String(summary || "").trim();
  if (!text) return "";
  return text.split(/\s*\/\s*/).map((part) => {
    if (part.startsWith("backfilled_talkers=")) {
      const matches = part.match(/['"][^'"]+['"]/g) || [];
      if (/\[\s*\]/.test(part)) return "回填对象=0个";
      return `回填对象=${matches.length || 1}个`;
    }
    if (part.startsWith("count=")) return `会话数=${part.slice("count=".length)}`;
    if (part.startsWith("source=")) return `源=${part.slice("source=".length)}`;
    if (part.startsWith("appended=")) return `源新增=${part.slice("appended=".length)}`;
    if (part.startsWith("processed=")) return `写入对话=${part.slice("processed=".length)}`;
    return part;
  }).join(" / ");
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
      body: JSON.stringify(weflowPayload({ talkers })),
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
  while (true) {
    await sleep(1000);
    const weflow = await api("/api/weflow/status");
    state.data = { ...(state.data || {}), weflow };
    renderWeFlow(weflow);
    const job = weflow.backfill_job || {};
    const progress = backfillProgress(job);
    helpers.update?.(progress.percent, progress.text);
    if (["completed", "cancelled", "error", "interrupted"].includes(String(job.status || ""))) {
      const result = weflow.last_backfill || job.result || job;
      showWeFlowStatusPayload(result);
      return {
        ...(typeof result === "object" && result ? result : {}),
        status: job.status === "completed" ? (result.status || "ok") : job.status,
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
  updateTask(taskId, {
    progress: ["completed", "cancelled", "error", "interrupted"].includes(String(job.status || "")) ? 99 : progress.percent,
    phase: progress.text,
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
  if (!window.confirm("将使用当前 Python 执行 pip install -r requirements-ocr.txt")) {
    return { status: "cancelled_by_user", message: "用户取消依赖安装" };
  }
  helpers.update?.(18, "正在安装可选依赖");
  const payload = await api("/api/weflow/install-deps", {
    method: "POST",
    body: JSON.stringify({ confirm_install: true }),
  });
  showWeFlowStatusPayload(compactPayload(payload, 6000));
  setStatusMessage("WeFlow 依赖安装完成");
  await refresh({ force: true });
  return payload;
}

function splitComma(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function actionButton(label, className, handler, meta = {}) {
  const button = document.createElement("button");
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", () => {
    runTask(
      {
        label: meta.label || label,
        category: meta.category || "操作",
        scope: meta.scope || "global",
        scopeLabel: meta.scopeLabel,
        target: meta.target || "",
        button,
      },
      (helpers) => handler(helpers),
    );
  });
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
  button.addEventListener("click", (event) => {
    const resolved = typeof meta === "function" ? meta(event) : meta;
    runTask(
      {
        ...(resolved || {}),
        button: event.currentTarget,
      },
      (helpers) => handler(helpers, event),
    );
  });
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
  $("#diagnosticsPage").hidden = page !== "diagnostics";
  if (page === "diagnostics") loadKeyPool();
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
  button.disabled = status === "saving" || Boolean(button.dataset.taskLocked);
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
    bridge_outbox_available: "bridge_outbox 可入队",
    bridge_outbox_configured_disabled: "bridge_outbox 已配置，发送未启用",
    bridge_outbox_ready: "bridge_outbox 已启用",
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

function channelDisplayName(channel) {
  const candidates = [
    channel.chat_title,
    ...(Array.isArray(channel.sender_names) ? channel.sender_names : []),
  ];
  const name = candidates
    .map((item) => String(item || "").trim())
    .find((item) => item && !looksLikeInternalConversationId(item) && !looksLikeMojibakeText(item));
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

const keyPoolState = { keys: [], keyFile: "", writable: false, loading: false };

async function loadKeyPool() {
  if (keyPoolState.loading) return;
  keyPoolState.loading = true;
  try {
    const payload = await api("/api/keys");
    applyKeyPoolPayload(payload);
  } catch (error) {
    setKeyPoolMessage(`加载密钥池失败：${error.message}`, true);
  } finally {
    keyPoolState.loading = false;
  }
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
  info.appendChild(preview);
  info.appendChild(meta);
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
  if (!value) {
    setKeyPoolMessage("请先粘贴密钥", true);
    return { status: "error" };
  }
  helpers.update?.(30, "正在写入密钥池");
  try {
    const payload = await api("/api/keys/add", { method: "POST", body: JSON.stringify({ value }) });
    if (input) input.value = "";
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
  helpers.update?.(30, "正在移除密钥");
  try {
    const payload = await api("/api/keys/remove", { method: "POST", body: JSON.stringify({ ref }) });
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
bindTaskButton("#probeButton", {
  label: "探测微信窗口",
  category: "诊断",
  scope: "diagnostic:wechat-probe",
}, (helpers) => {
  helpers.update(28, "正在探测微信窗口句柄");
  return probeNow();
});
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
}, async (helpers) => {
  helpers.update(30, "正在清空 WeFlow 操作历史");
  const payload = await api("/api/weflow/clear-history", { method: "POST", body: JSON.stringify({}) });
  showWeFlowStatusPayload(payload);
  setStatusMessage("操作历史已清空");
  await refresh({ force: true });
  return payload;
});
bindTaskButton("#weflowPullButton", {
  label: "WeFlow 拉取一次",
  category: "WeFlow",
  scope: "weflow:exclusive",
}, (helpers) => weflowAction("pull-once", {}, helpers));
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
$("#toggleProbe").addEventListener("click", () => {
  state.probeExpanded = !state.probeExpanded;
  renderProbeJson();
});
bindTaskButton("#refreshKeys", {
  label: "刷新密钥池",
  category: "密钥池",
  scope: "settings:key-pool",
}, (helpers) => {
  helpers.update(30, "正在读取密钥池");
  return loadKeyPool();
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
