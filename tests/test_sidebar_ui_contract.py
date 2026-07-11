from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDEBAR_DIR = ROOT / "app" / "personal_wechat_bot" / "ui" / "sidebar"


def _javascript_function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(f"JavaScript function not found: {name}")
    body_match = re.search(r"\)\s*\{", source[match.end() :])
    if body_match is None:
        raise AssertionError(f"JavaScript function has no body: {name}")
    brace = match.end() + body_match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = brace
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(f"JavaScript function body is not balanced: {name}")


def _run_node(script: str, *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is required for the sidebar JavaScript contract")
    return subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class SidebarUiContractTest(unittest.TestCase):
    def test_model_suggestions_replace_previous_options_without_duplicates(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        set_suggestions = _javascript_function(js, "setModelSuggestions")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const options = [];
            const datalist = {{
              append(option) {{ options.push(option); }},
              replaceChildren() {{ options.length = 0; }},
            }};
            const document = {{
              createElement(tag) {{
                assert.equal(tag, "option");
                return {{ value: "", label: "" }};
              }},
            }};
            const MODEL_QUICK_OPTIONS = [
              {{ value: "model-a", label: "A" }},
              {{ value: "model-b", label: "B" }},
            ];
            function $(selector) {{
              assert.equal(selector, "#modelNameOptions");
              return datalist;
            }}
            {set_suggestions}
            setModelSuggestions(["model-b", "model-c", "model-c"]);
            assert.deepEqual(options.map((item) => item.value), ["model-a", "model-b", "model-c"]);
            setModelSuggestions(["model-c"]);
            assert.deepEqual(options.map((item) => item.value), ["model-a", "model-b", "model-c"]);
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_button_progress_popover_moves_below_page_tabs(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        position = _javascript_function(js, "positionButtonTaskProgress")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const shell = {{ getBoundingClientRect: () => ({{ left: 0, right: 1440 }}) }};
            const tabs = {{
              offsetParent: {{}},
              getBoundingClientRect: () => ({{ top: 70, bottom: 108 }}),
            }};
            const button = {{
              isConnected: true,
              offsetParent: {{}},
              getBoundingClientRect: () => ({{ left: 1360, top: 20, bottom: 50, width: 40 }}),
            }};
            const values = {{}};
            const node = {{
              hidden: true,
              getBoundingClientRect: () => ({{ width: 250, height: 80 }}),
              style: {{
                setProperty: (name, value) => {{ values[name] = value; }},
                set left(value) {{ values.left = value; }},
                set top(value) {{ values.top = value; }},
              }},
            }};
            const window = {{ innerHeight: 1000 }};
            function $(selector) {{
              if (selector === ".shell") return shell;
              if (selector === ".page-tabs") return tabs;
              throw new Error(`unexpected selector ${{selector}}`);
            }}
            {position}
            positionButtonTaskProgress(button, node);
            assert.equal(node.hidden, false);
            assert.equal(values.top, "116px");
            assert.ok(Number.parseInt(values.top, 10) >= 108 + 8);
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_javascript_referenced_ids_exist_in_sidebar_html(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        selector_ids = set(re.findall(r'["\']#([A-Za-z0-9_-]+)', js))

        missing = sorted(selector_ids - html_ids)

        self.assertEqual(missing, [])

    def test_navigation_buttons_have_matching_panels_and_statuses(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        pages = re.findall(r'<button[^>]+data-page="([^"]+)"', html)
        panels = re.findall(r'<button[^>]+data-panel="([^"]+)"', html)
        statuses = re.findall(r'<button[^>]+data-status="([^"]+)"', html)

        self.assertEqual(sorted(f"{page}Page" for page in pages if f"{page}Page" not in html_ids), [])
        self.assertEqual(sorted(f"{panel}Panel" for panel in panels if f"{panel}Panel" not in html_ids), [])
        self.assertEqual(
            statuses,
            ["pending", "approved", "queued_to_bridge", "accepted", "rejected", "sent", "failed"],
        )

    def test_id_buttons_are_bound_or_are_form_submit_buttons(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        button_ids = set(re.findall(r'<button[^>]+id="([^"]+)"', html))
        selector_ids = set(re.findall(r'["\']#([A-Za-z0-9_-]+)', js))
        submit_buttons = {
            match.group(1)
            for match in re.finditer(r'<button[^>]+id="([^"]+)"[^>]+type="submit"', html)
        }

        unbound = sorted(button_ids - selector_ids - submit_buttons)

        self.assertEqual(unbound, [])

    def test_storage_status_prioritizes_database_contracts_before_component_compaction(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        helper = re.search(
            r"function storageStatusDisplayPayload\(payload\) \{(?P<body>.*?)\n\}",
            js,
            flags=re.DOTALL,
        )

        self.assertIsNotNone(helper)
        body = helper.group("body")
        self.assertIn("database_contract_summary: payload?.database_contract_summary", body)
        self.assertIn("database_contracts: Array.isArray(payload?.database_contracts)", body)
        self.assertIn("components: compactPayload", body)
        self.assertNotIn("database_contracts: compactPayload", body)

    def test_api_timeout_covers_response_body_read(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the sidebar JavaScript contract")
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("async function api(")
        end = js.index("async function refresh(", start)
        api_source = js[start:end]
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            {api_source}
            global.fetch = async (_path, options) => ({{
              ok: true,
              status: 200,
              text: () => new Promise((_resolve, reject) => {{
                options.signal.addEventListener("abort", () => {{
                  const error = new Error("aborted");
                  error.name = "AbortError";
                  reject(error);
                }}, {{ once: true }});
              }}),
            }});
            const watchdog = setTimeout(() => process.exit(3), 3000);
            (async () => {{
              const started = Date.now();
              try {{
                await api("/slow-body", {{ timeoutMs: 1000 }});
                process.exitCode = 2;
              }} catch (error) {{
                assert.match(error.message, /请求超时：\\/slow-body/);
                const elapsed = Date.now() - started;
                assert.ok(elapsed >= 900 && elapsed < 2500, `unexpected timeout ${{elapsed}}ms`);
              }} finally {{
                clearTimeout(watchdog);
              }}
            }})();
            """
        )

        completed = subprocess.run(
            [node, "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_destructive_sidebar_actions_require_confirmation(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        function_names = (
            "clearSendAudit",
            "clearWeFlowHistory",
            "clearHistoryData",
            "removeQueueItem",
            "deleteChannel",
            "cleanupHiddenChannels",
            "ackBridge",
            "retryBridge",
            "clearChannelPersonaOverride",
            "clearChannelSkillsOverride",
            "weflowInstallDeps",
            "removeKey",
        )
        for name in function_names:
            with self.subTest(function=name):
                start = js.index(f"async function {name}(")
                next_starts = [
                    position
                    for marker in ("\nasync function ", "\nfunction ")
                    if (position := js.find(marker, start + 1)) >= 0
                ]
                end = min(next_starts) if next_starts else len(js)
                self.assertIn("window.confirm", js[start:end])

    def test_model_refresh_propagates_key_pool_failure_and_deduplicates_loads(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        load_key_pool = _javascript_function(js, "loadKeyPool")
        refresh_model = _javascript_function(js, "refreshModelConfiguration")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const keyPoolState = {{
              keys: [], keyFile: "", writable: false, loading: false, requestEpoch: 0, loadPromise: null,
              inputRevision: 0,
            }};
            const modelConfigState = {{ revision: 0 }};
            let apiCalls = 0;
            let keyMessage = "";
            let modelMessage = "";
            let statusMessage = "";
            async function api(path) {{
              assert.equal(path, "/api/keys");
              apiCalls += 1;
              await Promise.resolve();
              throw new Error("key_pool_offline");
            }}
            function applyKeyPoolPayload() {{ throw new Error("unexpected success"); }}
            function setKeyPoolMessage(message) {{ keyMessage = message; }}
            function clearModelConfigTouched() {{}}
            async function loadModelConfig() {{ return {{ status: "ok", model: "saved" }}; }}
            function setModelConfigMessage(message) {{ modelMessage = message; }}
            function setStatusMessage(message) {{ statusMessage = message; }}
            {load_key_pool}
            {refresh_model}
            (async () => {{
              const [first, second] = await Promise.all([loadKeyPool(), loadKeyPool()]);
              assert.equal(apiCalls, 1);
              assert.equal(first.status, "error");
              assert.equal(second.status, "error");
              assert.equal(keyPoolState.loading, false);
              assert.equal(keyPoolState.loadPromise, null);
              const updates = [];
              const refreshed = await refreshModelConfiguration({{ update: (progress) => updates.push(progress) }});
              assert.equal(apiCalls, 2);
              assert.equal(refreshed.status, "error");
              assert.equal(refreshed.model_config.status, "ok");
              assert.equal(refreshed.key_pool.status, "error");
              assert.deepEqual(updates, [25, 65]);
              assert.equal(modelConfigState.revision, 1);
              assert.match(keyMessage, /key_pool_offline/);
              assert.match(modelMessage, /key_pool_offline/);
              assert.match(statusMessage, /key_pool_offline/);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_key_add_preserves_a_new_secret_typed_while_post_is_pending(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        add_key = _javascript_function(js, "addKey")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const keyPoolState = {{
              keys: [], keyFile: "", writable: false, loading: false, requestEpoch: 0, loadPromise: null,
              inputRevision: 7,
            }};
            const input = {{ value: "first-secret" }};
            let resolveMutation;
            const mutation = new Promise((resolve) => {{ resolveMutation = resolve; }});
            function $(selector) {{ assert.equal(selector, "#newKeyValue"); return input; }}
            async function api(path) {{ assert.equal(path, "/api/keys/add"); return mutation; }}
            function applyKeyPoolPayload() {{}}
            function setKeyPoolMessage() {{}}
            function setStatusMessage() {{}}
            {add_key}
            (async () => {{
              const pending = addKey();
              input.value = "second-secret";
              keyPoolState.inputRevision += 1;
              resolveMutation({{ status: "ok" }});
              await pending;
              assert.equal(input.value, "second-secret");
              assert.equal(keyPoolState.inputRevision, 8);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_model_save_result_fences_get_started_while_post_is_pending(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        load_model = _javascript_function(js, "loadModelConfig")
        save_model = _javascript_function(js, "saveModelConfig")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const modelConfigState = {{
              loading: false,
              loaded: false,
              revision: 4,
              requestEpoch: 0,
              loadPromise: null,
            }};
            const pending = {{}};
            const applied = [];
            function modelConfigPayload() {{
              return {{ provider: "relay", model: "saved-model", base_url: "http://saved" }};
            }}
            function applyModelConfig(payload, options) {{ applied.push([payload.model, options.force]); }}
            function setModelConfigMessage() {{}}
            function setStatusMessage() {{}}
            function api(path, options = {{}}) {{
              if (options.method === "POST") {{
                return new Promise((resolve) => {{ pending.post = resolve; }});
              }}
              return new Promise((resolve) => {{ pending.get = resolve; }});
            }}
            {load_model}
            {save_model}
            (async () => {{
              const save = saveModelConfig();
              const staleLoad = loadModelConfig();
              pending.post({{
                status: "ok",
                model_config: {{ model: "saved-model" }},
              }});
              await save;
              assert.deepEqual(applied, [["saved-model", true]]);
              pending.get({{ model: "pre-save-model" }});
              await staleLoad;
              assert.deepEqual(applied, [["saved-model", true]]);
              assert.equal(modelConfigState.requestEpoch, 3);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_forced_model_load_does_not_overwrite_edit_made_while_waiting(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        load_model = _javascript_function(js, "loadModelConfig")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const modelConfigState = {{
              loading: false,
              loaded: false,
              revision: 0,
              requestEpoch: 0,
              loadPromise: null,
            }};
            const pending = [];
            const applied = [];
            function api() {{ return new Promise((resolve) => pending.push(resolve)); }}
            function applyModelConfig(payload, options) {{ applied.push([payload.model, options.force]); }}
            function setModelConfigMessage() {{}}
            {load_model}
            (async () => {{
              const initial = loadModelConfig();
              const forced = loadModelConfig({{ force: true }});
              modelConfigState.revision += 1;
              pending[0]({{ model: "first-server-snapshot" }});
              await initial;
              await new Promise((resolve) => setImmediate(resolve));
              assert.equal(pending.length, 2);
              pending[1]({{ model: "second-server-snapshot" }});
              await forced;
              assert.deepEqual(applied, [["second-server-snapshot", false]]);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_model_probe_ignores_changed_config_and_superseded_responses(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        probe_model = _javascript_function(js, "probeModelFetch")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const modelConfigState = {{ revision: 4, probeRequestEpoch: 0, probeModels: [] }};
            const box = {{ hidden: false, textContent: "old-result" }};
            let currentPayload = {{ provider: "relay", model: "model-a", base_url: "https://a.example/v1" }};
            const requests = [];
            const applied = [];
            const messages = [];
            function $ (selector) {{ assert.equal(selector, "#modelProbeResult"); return box; }}
            function modelConfigPayload() {{ return {{ ...currentPayload }}; }}
            function api(path, options) {{
              assert.equal(path, "/api/model-config/probe");
              assert.equal(options.method, "POST");
              return new Promise((resolve, reject) => requests.push({{ resolve, reject }}));
            }}
            function renderModelProbe(result) {{ applied.push(result.marker); }}
            function setModelConfigMessage(message) {{ messages.push(message); }}
            {probe_model}
            (async () => {{
              const changed = probeModelFetch();
              assert.equal(box.hidden, true);
              currentPayload = {{ ...currentPayload, base_url: "https://b.example/v1" }};
              modelConfigState.revision += 1;
              requests[0].resolve({{ status: "ok", model_count: 1, marker: "stale-edit" }});
              const changedResult = await changed;
              assert.equal(changedResult.ignored, true);
              assert.equal(changedResult.ignore_reason, "stale_model_probe_response");
              assert.deepEqual(applied, []);
              assert.match(messages.at(-1), /旧探测结果已忽略/);

              const superseded = probeModelFetch();
              const latest = probeModelFetch();
              requests[2].resolve({{ status: "ok", model_count: 1, marker: "latest" }});
              await latest;
              requests[1].resolve({{ status: "ok", model_count: 1, marker: "superseded" }});
              const supersededResult = await superseded;
              assert.equal(supersededResult.ignored, true);
              assert.deepEqual(applied, ["latest"]);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_lane_composition_and_three_refresh_draft_pruning_state_machine(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        set_composition = _javascript_function(js, "setChannelLaneComposition")
        composition_active = _javascript_function(js, "channelLaneCompositionActive")
        prune_drafts = _javascript_function(js, "pruneMissingChannelLaneDrafts")
        render_lanes = _javascript_function(js, "renderChannelTaskLanes")
        self.assertLess(render_lanes.index("channelLaneCompositionActive()"), render_lanes.index('list.innerHTML = ""'))
        self.assertIn('addEventListener("compositionstart"', js)
        self.assertIn('addEventListener("compositionend"', js)
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{
              channelLaneComposing: new Set(),
              channelLaneMissingRefreshes: new Map(),
              channelLaneDrafts: new Map(),
              channelLaneOpenState: new Map(),
              channelLaneDraftPruneRevision: 0,
              successfulRefreshRevision: 0,
            }};
            {set_composition}
            {composition_active}
            {prune_drafts}
            setChannelLaneComposition("lane-a", true);
            assert.equal(channelLaneCompositionActive(), true);
            setChannelLaneComposition("lane-a", false);
            assert.equal(channelLaneCompositionActive(), false);
            state.channelLaneDrafts.set("lane-a", {{ revision: 1 }});
            state.channelLaneOpenState.set("lane-a", true);
            state.successfulRefreshRevision = 1;
            pruneMissingChannelLaneDrafts([]);
            pruneMissingChannelLaneDrafts([]);
            assert.equal(state.channelLaneMissingRefreshes.get("lane-a"), 1);
            state.successfulRefreshRevision = 2;
            pruneMissingChannelLaneDrafts([]);
            assert.equal(state.channelLaneDrafts.has("lane-a"), true);
            state.successfulRefreshRevision = 3;
            pruneMissingChannelLaneDrafts([]);
            assert.equal(state.channelLaneDrafts.has("lane-a"), false);
            assert.equal(state.channelLaneOpenState.has("lane-a"), false);

            state.channelLaneDrafts.set("lane-b", {{ revision: 1 }});
            state.successfulRefreshRevision = 4;
            pruneMissingChannelLaneDrafts([]);
            state.successfulRefreshRevision = 5;
            pruneMissingChannelLaneDrafts([{{ conversation_id: "lane-b" }}]);
            state.successfulRefreshRevision = 6;
            pruneMissingChannelLaneDrafts([]);
            state.successfulRefreshRevision = 7;
            pruneMissingChannelLaneDrafts([]);
            assert.equal(state.channelLaneDrafts.has("lane-b"), true);
            state.successfulRefreshRevision = 8;
            pruneMissingChannelLaneDrafts([]);
            assert.equal(state.channelLaneDrafts.has("lane-b"), false);
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_key_mutation_response_fences_a_later_stale_get(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        load_key_pool = _javascript_function(js, "loadKeyPool")
        add_key = _javascript_function(js, "addKey")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const keyPoolState = {{
              keys: [], keyFile: "", writable: false, loading: false, requestEpoch: 0, loadPromise: null,
              inputRevision: 0,
            }};
            const input = {{ value: "new-secret" }};
            const applied = [];
            let resolveMutation;
            let resolveGet;
            const mutation = new Promise((resolve) => {{ resolveMutation = resolve; }});
            const staleGet = new Promise((resolve) => {{ resolveGet = resolve; }});
            function $(selector) {{ assert.equal(selector, "#newKeyValue"); return input; }}
            async function api(path) {{
              if (path === "/api/keys/add") return mutation;
              if (path === "/api/keys") return staleGet;
              throw new Error(`unexpected path ${{path}}`);
            }}
            function applyKeyPoolPayload(payload) {{ applied.push(payload.marker); }}
            function setKeyPoolMessage() {{}}
            function setStatusMessage() {{}}
            {load_key_pool}
            {add_key}
            (async () => {{
              const mutationCall = addKey();
              const getCall = loadKeyPool({{ force: true }});
              resolveMutation({{ status: "ok", marker: "mutation" }});
              const mutationResult = await mutationCall;
              assert.equal(mutationResult.marker, "mutation");
              assert.deepEqual(applied, ["mutation"]);
              resolveGet({{ status: "ok", marker: "stale-get" }});
              const getResult = await getCall;
              assert.equal(getResult.marker, "stale-get");
              assert.deepEqual(applied, ["mutation"]);
              assert.equal(input.value, "");
              assert.equal(keyPoolState.requestEpoch, 3);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_lane_display_overlays_only_dirty_draft_fields(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        display_values = _javascript_function(js, "channelLaneControlDisplayValues")
        render_lane = _javascript_function(js, "renderLaneControl")
        self.assertIn("channelLaneControlDisplayValues(control, draft)", render_lane)
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            function datetimeLocalValue(value) {{ return String(value || ""); }}
            {display_values}
            const control = {{
              pinned: true,
              priority: 80,
              snoozed_until: "server-snooze",
              wait_reason: "server-wait",
              operator_note: "server-note",
            }};
            const draft = {{
              dirtyFields: ["priority"],
              values: {{
                pinned: false,
                priority: "35",
                snoozedUntil: "stale-snooze",
                waitReason: "stale-wait",
                operatorNote: "stale-note",
              }},
            }};
            const values = channelLaneControlDisplayValues(control, draft);
            assert.deepEqual(values, {{
              pinned: true,
              priority: "35",
              snoozedUntil: "server-snooze",
              waitReason: "server-wait",
              operatorNote: "server-note",
            }});
            draft.dirtyFields = ["pinned", "waitReason"];
            const second = channelLaneControlDisplayValues(control, draft);
            assert.equal(second.pinned, false);
            assert.equal(second.waitReason, "stale-wait");
            assert.equal(second.priority, "80");
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_resume_reconciles_wait_and_snooze_by_independent_field_revision(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        action_fields = _javascript_function(js, "channelControlActionDraftFields")
        reconcile_many = _javascript_function(js, "reconcileChannelLaneActionDrafts")
        reconcile_one = _javascript_function(js, "reconcileChannelLaneActionDraft")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ channelLaneDrafts: new Map() }};
            function configBoolean(value) {{ return Boolean(value); }}
            function datetimeLocalValue(value) {{ return String(value || ""); }}
            {action_fields}
            {reconcile_many}
            {reconcile_one}
            const fields = channelControlActionDraftFields("resume");
            assert.deepEqual(fields, ["waitReason", "snoozedUntil"]);
            state.channelLaneDrafts.set("lane-a", {{
              revision: 3,
              values: {{ waitReason: "old wait", snoozedUntil: "old snooze", operatorNote: "keep" }},
              dirtyFields: ["waitReason", "snoozedUntil", "operatorNote"],
              fieldRevisions: {{ waitReason: 1, snoozedUntil: 2, operatorNote: 3 }},
            }});
            const expected = {{ waitReason: 1, snoozedUntil: 2 }};
            const edited = state.channelLaneDrafts.get("lane-a");
            edited.values.waitReason = "typed after resume";
            edited.fieldRevisions.waitReason = 4;
            edited.revision = 4;
            reconcileChannelLaneActionDrafts(
              "lane-a",
              fields,
              expected,
              {{ wait_reason: "", snoozed_until: "" }},
            );
            const result = state.channelLaneDrafts.get("lane-a");
            assert.equal(result.values.waitReason, "typed after resume");
            assert.equal(result.values.snoozedUntil, "");
            assert.deepEqual(result.dirtyFields, ["waitReason", "operatorNote"]);
            assert.equal(result.fieldRevisions.waitReason, 4);
            assert.equal("snoozedUntil" in result.fieldRevisions, false);
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_channel_save_reconciles_each_submitted_field_revision(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        save_lane = _javascript_function(js, "saveChannelLaneControl")
        reconcile_many = _javascript_function(js, "reconcileChannelLaneActionDrafts")
        reconcile_one = _javascript_function(js, "reconcileChannelLaneActionDraft")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ channelLaneDrafts: new Map() }};
            const submittedValues = {{
              pinned: false,
              priority: "40",
              waitReason: "",
              operatorNote: "submitted note",
              snoozedUntil: "",
            }};
            let resolveUpdate;
            let submittedPayload;
            let renders = 0;
            function channelLaneControlValues() {{ return submittedValues; }}
            function updateChannelControl(conversationId, payload) {{
              assert.equal(conversationId, "lane-a");
              submittedPayload = payload;
              return new Promise((resolve) => {{ resolveUpdate = resolve; }});
            }}
            function renderChannelTaskLanes() {{ renders += 1; }}
            function datetimeLocalToIso(value) {{ return value; }}
            {reconcile_many}
            {reconcile_one}
            {save_lane}
            state.channelLaneDrafts.set("lane-a", {{
              revision: 2,
              values: {{ ...submittedValues }},
              dirtyFields: ["priority", "operatorNote"],
              fieldRevisions: {{ priority: 1, operatorNote: 2 }},
            }});
            (async () => {{
              const save = saveChannelLaneControl("lane-a", {{}});
              assert.deepEqual(submittedPayload.patch, {{ priority: 40, operator_note: "submitted note" }});
              const edited = state.channelLaneDrafts.get("lane-a");
              edited.revision = 3;
              edited.values.operatorNote = "edited while saving";
              edited.fieldRevisions.operatorNote = 3;
              resolveUpdate({{
                channel_state: {{ control: {{ priority: 40, operator_note: "submitted note" }} }},
              }});
              await save;
              const draft = state.channelLaneDrafts.get("lane-a");
              assert.deepEqual(draft.dirtyFields, ["operatorNote"]);
              assert.equal(draft.values.priority, "40");
              assert.equal(draft.values.operatorNote, "edited while saving");
              assert.deepEqual(draft.fieldRevisions, {{ operatorNote: 3 }});
              assert.equal(renders, 1);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_bridge_manual_ack_conflict_is_not_reported_as_success(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        ack_bridge = _javascript_function(js, "ackBridge")
        task_failed = _javascript_function(js, "taskResultFailed")
        self.assertIn("待桥接（queued）", ack_bridge)
        self.assertIn("inflight", ack_bridge)
        self.assertIn("陈旧请求会被拒绝", ack_bridge)
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const window = {{ confirm: () => true }};
            let statusMessage = "";
            let refreshOptions = null;
            async function api() {{
              return {{ status: "conflict", applied: false, effective_status: "inflight" }};
            }}
            function bridgeStatusText(status) {{ return status === "inflight" ? "发送中" : status; }}
            function setStatusMessage(message) {{ statusMessage = message; }}
            async function refresh(options) {{ refreshOptions = options; return {{ status: "ok" }}; }}
            {ack_bridge}
            {task_failed}
            (async () => {{
              const result = await ackBridge("bridge-1", "failed");
              assert.equal(result.status, "conflict");
              assert.equal(result.applied, false);
              assert.equal(taskResultFailed(result), true);
              assert.match(statusMessage, /无法取消/);
              assert.deepEqual(refreshOptions, {{ force: true }});
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_passive_backend_probe_status_is_visible_as_a_warning(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        status_text = _javascript_function(js, "backgroundSendText")
        status_tone = _javascript_function(js, "bridgeStatusTone")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            {status_text}
            {status_tone}
            assert.equal(
              backgroundSendText("bridge_outbox_backend_probe_deferred"),
              "worker 运行中；后端健康等待主动探测",
            );
            assert.equal(bridgeStatusTone("bridge_outbox_backend_probe_deferred"), "warn");
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_unchecked_window_probe_is_not_rendered_as_not_found(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        render_probe = _javascript_function(js, "renderWechatProbe")
        status_text = _javascript_function(js, "probeStatusText")
        script = (
            textwrap.dedent(
                """
                const assert = require("node:assert/strict");
                const nodes = {
                  diagnostic: { textContent: "" },
                  list: { innerHTML: "", children: [], append(value) { this.children.push(value); } },
                };
                function $(selector) { return selector === "#diagnosticDetail" ? nodes.diagnostic : nodes.list; }
                function emptyNode(text) { return { textContent: text }; }
                """
            )
            + render_probe
            + "\n"
            + status_text
            + textwrap.dedent(
                """
                renderWechatProbe({
                  status: "unchecked",
                  active: { status: "unchecked" },
                  windows: [],
                  ui_automation: { available: false, reason: "explicit_probe_required" },
                });
                assert.match(nodes.diagnostic.textContent, /尚未主动探测/);
                assert.doesNotMatch(nodes.diagnostic.textContent, /未发现可用/);
                assert.equal(nodes.list.children[0].textContent, "尚未主动探测微信窗口");
                assert.equal(probeStatusText("unchecked"), "尚未主动探测");
                """
            )
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_active_window_probe_overlay_survives_passive_refresh_and_rejects_old_response(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        effective_probe = _javascript_function(js, "effectiveWechatProbe")
        invalidate_probe = _javascript_function(js, "invalidateWechatProbeOverlay")
        probe_now = _javascript_function(js, "probeNow")
        script = (
            textwrap.dedent(
                """
                const assert = require("node:assert/strict");
                const state = {
                  data: { wechat_window_probe: { status: "unchecked", source: "passive-1" } },
                  dataEpoch: 3,
                  wechatProbeOverlay: null,
                  wechatProbeRequestEpoch: 0,
                };
                const pending = [];
                let rejectNextProbe = false;
                function api(path, options) {
                  assert.equal(path, "/api/wechat-probe");
                  assert.equal(options.method, "POST");
                  if (rejectNextProbe) {
                    rejectNextProbe = false;
                    return Promise.reject(new Error("probe_failed"));
                  }
                  return new Promise((resolve) => pending.push(resolve));
                }
                function renderWechatProbe() {}
                function renderProbeJson() {}
                """
            )
            + effective_probe
            + "\n"
            + invalidate_probe
            + "\n"
            + probe_now
            + textwrap.dedent(
                """
                (async () => {
                  const oldRequest = probeNow();
                  const newRequest = probeNow();
                  state.dataEpoch += 1;
                  state.data = { wechat_window_probe: { status: "unchecked", source: "passive-during-request" } };
                  pending[1]({ status: "ok", active: { status: "matched_foreground", title: "new" }, windows: [] });
                  await newRequest;
                  state.data = { wechat_window_probe: { status: "unchecked", source: "passive-2" } };
                  assert.equal(effectiveWechatProbe().active.title, "new");
                  pending[0]({ status: "not_found", active: { status: "not_found", title: "old" }, windows: [] });
                  const oldResult = await oldRequest;
                  assert.equal(oldResult.ignored, true);
                  assert.equal(effectiveWechatProbe().active.title, "new");
                  assert.equal(state.data.wechat_window_probe.source, "passive-2");
                  state.dataEpoch += 1;
                  assert.equal(effectiveWechatProbe().active.title, "new");
                  rejectNextProbe = true;
                  await assert.rejects(probeNow(), /probe_failed/);
                  assert.equal(effectiveWechatProbe().source, "passive-2");
                  invalidateWechatProbeOverlay();
                  assert.equal(effectiveWechatProbe().source, "passive-2");
                })().catch((error) => { console.error(error); process.exitCode = 1; });
                """
            )
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_driver_probe_requires_saved_config_and_expires_by_fingerprint_and_ttl(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        functions = "\n".join(
            _javascript_function(js, name)
            for name in (
                "configBoolean",
                "selectedSendBackend",
                "driverProbeConfigFingerprint",
                "driverProbePayloadFingerprint",
                "currentDriverProbeOverlay",
                "invalidateDriverProbeOverlay",
                "driverProbeSummary",
                "probeDriverNow",
            )
        )
        script = textwrap.dedent(
            """
            const assert = require("node:assert/strict");
            const DRIVER_PROBE_TTL_MS = 60000;
            const nativeConfig = {
              send_driver: "bridge_outbox",
              send_enabled: true,
              send_backend: "wechat_native_http",
              weflow_base_url: "http://127.0.0.1:5031",
              weflow_token_env: "WEFLOW_API_TOKEN",
              weflow_send_text_path: "/send/text",
              weflow_send_file_path: "/send/file",
              wechat_native_base_url: "http://127.0.0.1:30001",
              wechat_native_send_text_path: "/SendTextMsg",
              wechat_native_send_image_path: "/SendImgMsg",
              wechat_native_send_file_path: "/send_file_msg",
              wechat_native_status_path: "/QueryDB/status",
            };
            const state = {
              data: { config: { ...nativeConfig }, driver_probe: { source: "passive" } },
              dataEpoch: 7,
              controlsDirty: true,
              controlsSaving: false,
              driverProbeOverlay: null,
              driverProbeRequestEpoch: 0,
            };
            const requests = [];
            let rejectNextProbe = false;
            function api(path, options) {
              assert.equal(path, "/api/driver-probe");
              assert.equal(options.timeoutMs, 10000);
              if (rejectNextProbe) {
                rejectNextProbe = false;
                return Promise.reject(new Error("probe_failed"));
              }
              return new Promise((resolve) => requests.push(resolve));
            }
            function setStatusMessage() {}
            function renderSendControlSummary() {}
            function renderProbeJson() {}
            function probePayload(overrides = {}) {
              return {
                status: "ok",
                probe: {
                  configured_driver: nativeConfig.send_driver,
                  send_enabled: nativeConfig.send_enabled,
                  send_backend: nativeConfig.send_backend,
                  weflow_base_url: nativeConfig.weflow_base_url,
                  weflow_token_env: nativeConfig.weflow_token_env,
                  weflow_send_text_path: nativeConfig.weflow_send_text_path,
                  weflow_send_file_path: nativeConfig.weflow_send_file_path,
                  wechat_native_base_url: nativeConfig.wechat_native_base_url,
                  wechat_native_send_text_path: nativeConfig.wechat_native_send_text_path,
                  wechat_native_send_image_path: nativeConfig.wechat_native_send_image_path,
                  wechat_native_send_file_path: nativeConfig.wechat_native_send_file_path,
                  wechat_native_status_path: nativeConfig.wechat_native_status_path,
                  driver_probe: { health: "ready", blockers: [] },
                  ...overrides,
                },
              };
            }
            """
        ) + functions + textwrap.dedent(
            """
            (async () => {
              const blocked = await probeDriverNow();
              assert.equal(blocked.reason, "unsaved_send_controls");
              assert.equal(requests.length, 0);

              state.controlsDirty = false;
              const staleRequest = probeDriverNow();
              state.data = { config: { ...nativeConfig, send_backend: "weflow_http" } };
              requests[0](probePayload());
              const stale = await staleRequest;
              assert.equal(stale.ignored, true);
              assert.equal(state.driverProbeOverlay, null);

              state.data = { config: { ...nativeConfig }, driver_probe: { source: "passive-new" } };
              const mismatchedResponseRequest = probeDriverNow();
              requests[1](probePayload({ send_backend: "weflow_http" }));
              const mismatched = await mismatchedResponseRequest;
              assert.equal(mismatched.ignored, true);
              assert.equal(state.driverProbeOverlay, null);

              const validRequest = probeDriverNow();
              state.dataEpoch += 1;
              state.data = { config: { ...nativeConfig }, driver_probe: { source: "passive-during-request" } };
              requests[2](probePayload());
              await validRequest;
              const overlay = state.driverProbeOverlay;
              assert.ok(overlay);
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), overlay);
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt), null);
              assert.equal(state.data.driver_probe.source, "passive-during-request");

              state.dataEpoch += 1;
              state.data = { config: { ...nativeConfig }, driver_probe: { source: "passive-after-probe" } };
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), overlay);

              state.data = { config: { ...nativeConfig, wechat_native_status_path: "/changed" } };
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), null);
              state.data = { config: { ...nativeConfig } };
              state.dataEpoch += 1;
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), overlay);
              rejectNextProbe = true;
              await assert.rejects(probeDriverNow(), /probe_failed/);
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), null);
              invalidateDriverProbeOverlay();
              assert.equal(currentDriverProbeOverlay(state.data, overlay.expiresAt - 1), null);
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_history_clear_explicitly_invalidates_probe_overlays(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        clear_history = _javascript_function(js, "clearHistoryData")

        self.assertIn("invalidateWechatProbeOverlay();", clear_history)
        self.assertIn("invalidateDriverProbeOverlay();", clear_history)

    def test_history_reset_status_reconciles_after_sidebar_reopen(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        reconcile = _javascript_function(js, "reconcileHistoryResetStatus")
        refresh_queue = _javascript_function(js, "drainRefreshQueue")
        self.assertIn("reconcileHistoryResetStatus(payload.history_reset);", refresh_queue)
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ historyResetPending: false, historyResetNoticeKey: "" }};
            const messages = [];
            let lockSyncs = 0;
            function setStatusMessage(message) {{ messages.push(message); }}
            function syncTaskButtonLocks() {{ lockSyncs += 1; }}
            {reconcile}

            reconcileHistoryResetStatus({{
              status: "running", phase: "clearing_history", active: true,
              terminal: false, outcome_unknown: false,
            }});
            assert.equal(state.historyResetPending, true);
            assert.match(messages.at(-1), /仍在进行/);

            reconcileHistoryResetStatus({{
              status: "ok", phase: "stopped_after_clear", active: false,
              terminal: true, updated_at: "2026-07-12T04:00:00Z",
              clear_result: {{ removed_count: 12, history_reset_id: "reset-1" }},
            }});
            assert.equal(state.historyResetPending, false);
            assert.match(messages.at(-1), /已完成/);
            const afterFirstTerminal = messages.length;
            reconcileHistoryResetStatus({{
              status: "ok", phase: "stopped_after_clear", active: false,
              terminal: true, updated_at: "2026-07-12T04:00:00Z",
              clear_result: {{ removed_count: 12, history_reset_id: "reset-1" }},
            }});
            assert.equal(messages.length, afterFirstTerminal);

            reconcileHistoryResetStatus({{
              status: "partial_error", phase: "clear_incomplete", active: false,
              terminal: true, updated_at: "2026-07-12T04:01:00Z",
              clear_result: {{ error_count: 3, history_reset_id: "reset-2" }},
            }});
            assert.match(messages.at(-1), /3 项失败/);

            reconcileHistoryResetStatus({{
              status: "unknown", phase: "status_unverifiable", active: true,
              terminal: false, outcome_unknown: true,
            }});
            assert.equal(state.historyResetPending, true);
            assert.match(messages.at(-1), /无法核实/);
            assert.ok(lockSyncs >= 4);
            """
        )

        completed = _run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_history_clear_scheduled_is_single_post_and_keeps_global_button_lock(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        clear_history = _javascript_function(js, "clearHistoryData")
        run_task = _javascript_function(js, "runTask")
        active_task = _javascript_function(js, "activeTaskForScope")
        sync_locks = _javascript_function(js, "syncTaskButtonLocks")
        sync_lock = _javascript_function(js, "syncTaskButtonLock")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const buttons = [
              {{ disabled: false, dataset: {{ taskScope: "history:clear" }} }},
              {{ disabled: false, dataset: {{ taskScope: "other:write" }} }},
            ];
            const state = {{
              historyResetPending: false,
              dataEpoch: 0,
              tasks: [],
              taskScopeChains: new Map(),
            }};
            const window = {{ confirm: () => true }};
            let posts = 0;
            let statusMessage = "";
            function $$() {{ return buttons; }}
            function invalidateWechatProbeOverlay() {{}}
            function invalidateDriverProbeOverlay() {{}}
            function setStatusMessage(message) {{ statusMessage = message; }}
            function updateButtonTaskProgress() {{}}
            async function refresh() {{ throw new Error("scheduled reset must not refresh"); }}
            async function api(path, options) {{
              assert.equal(path, "/api/history/clear");
              assert.equal(options.method, "POST");
              posts += 1;
              await Promise.resolve();
              return {{ status: "shutdown_scheduled" }};
            }}
            let taskSequence = 0;
            function createTask(meta) {{
              const task = {{ ...meta, id: `task-${{++taskSequence}}`, status: "queued" }};
              state.tasks.push(task);
              return task;
            }}
            async function executeTask(task, worker) {{
              task.status = "running";
              const result = await worker({{ update() {{}} }});
              task.status = taskResultFailed(result) ? "failed" : "completed";
              return result;
            }}
            function taskResultFailed(result) {{
              return ["error", "failed", "partial_error", "blocked", "conflict"]
                .includes(String(result?.status || ""));
            }}
            {active_task}
            {sync_lock}
            {sync_locks}
            {run_task}
            {clear_history}
            (async () => {{
              const meta = {{ scope: "history:clear" }};
              const first = runTask(meta, (helpers) => clearHistoryData(helpers));
              const second = runTask(meta, (helpers) => clearHistoryData(helpers));
              assert.equal(first, second);
              const [firstResult, secondResult] = await Promise.all([first, second]);
              assert.equal(firstResult.status, "shutdown_scheduled");
              assert.equal(secondResult.status, "shutdown_scheduled");
              assert.equal(posts, 1);
              assert.equal(state.historyResetPending, true);
              assert.deepEqual(buttons.map((button) => button.disabled), [true, true]);
              assert.match(statusMessage, /手动重新打开 sidebar/);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_api_history_reset_admission_block_locks_stale_tab(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        api_function = _javascript_function(js, "api")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ historyResetPending: false }};
            const buttons = [{{ disabled: false }}, {{ disabled: false }}];
            let statusMessage = "";
            let lockSyncs = 0;
            function setStatusMessage(message) {{ statusMessage = message; }}
            function syncTaskButtonLocks() {{
              lockSyncs += 1;
              for (const button of buttons) button.disabled = state.historyResetPending;
            }}
            async function fetch() {{
              return {{
                ok: false,
                status: 409,
                text: async () => JSON.stringify({{
                  status: "blocked",
                  error: "history_reset_in_progress",
                  history_reset_in_progress: true,
                  outcome_unknown: true,
                }}),
              }};
            }}
            {api_function}
            (async () => {{
              await assert.rejects(() => api("/api/model-config", {{ method: "POST", body: "{{}}" }}));
              assert.equal(state.historyResetPending, true);
              assert.equal(lockSyncs, 1);
              assert.deepEqual(buttons.map((button) => button.disabled), [true, true]);
              assert.ok(statusMessage.length > 0);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_history_clear_explicit_failures_and_proven_4xx_rejection_unlock(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        clear_history = _javascript_function(js, "clearHistoryData")
        task_failed = _javascript_function(js, "taskResultFailed")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ historyResetPending: false, dataEpoch: 0 }};
            const window = {{ confirm: () => true }};
            const responses = [
              {{ status: "blocked", message: "writer active" }},
              {{ status: "partial_error", error_count: 2 }},
              Object.assign(new Error("HTTP 409"), {{
                httpStatus: 409,
                payload: {{
                  status: "conflict",
                  message: "request rejected",
                  history_reset_not_scheduled: true,
                }},
              }}),
            ];
            const messages = [];
            let refreshes = 0;
            function invalidateWechatProbeOverlay() {{}}
            function invalidateDriverProbeOverlay() {{}}
            function syncTaskButtonLocks() {{}}
            function setStatusMessage(message) {{ messages.push(message); }}
            async function refresh(options) {{
              assert.deepEqual(options, {{ force: true }});
              refreshes += 1;
              return {{ status: "ok" }};
            }}
            async function api() {{
              const response = responses.shift();
              if (response instanceof Error) throw response;
              return response;
            }}
            {task_failed}
            {clear_history}
            (async () => {{
              const blocked = await clearHistoryData();
              assert.equal(blocked.status, "blocked");
              assert.equal(taskResultFailed(blocked), true);
              assert.equal(state.historyResetPending, false);
              const partial = await clearHistoryData();
              assert.equal(partial.status, "partial_error");
              assert.equal(taskResultFailed(partial), true);
              assert.equal(state.historyResetPending, false);
              const rejected = await clearHistoryData();
              assert.equal(rejected.status, "error");
              assert.equal(rejected.response_status, "conflict");
              assert.equal(taskResultFailed(rejected), true);
              assert.equal(state.historyResetPending, false);
              assert.equal(refreshes, 3);
              assert.match(messages[0], /历史清理被阻断/);
              assert.match(messages[1], /2 项删除失败/);
              assert.match(messages[2], /请求未受理/);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_history_clear_unknown_network_and_2xx_outcomes_remain_locked_failures(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        clear_history = _javascript_function(js, "clearHistoryData")
        task_failed = _javascript_function(js, "taskResultFailed")
        active_task = _javascript_function(js, "activeTaskForScope")
        sync_locks = _javascript_function(js, "syncTaskButtonLocks")
        sync_lock = _javascript_function(js, "syncTaskButtonLock")
        script = textwrap.dedent(
            f"""
            const assert = require("node:assert/strict");
            const state = {{ historyResetPending: false, dataEpoch: 0, tasks: [] }};
            const window = {{ confirm: () => true }};
            const buttons = [
              {{ disabled: false, dataset: {{ taskScope: "history:clear" }} }},
              {{ disabled: false, dataset: {{ taskScope: "other:write" }} }},
            ];
            const responses = [
              new Error("connection reset"),
              Object.assign(new Error("ambiguous HTTP 400"), {{
                httpStatus: 400,
                payload: {{ status: "blocked", message: "post-spawn failure" }},
              }}),
              {{ status: "failed", message: "ambiguous 2xx" }},
              {{ status: "conflict" }},
              {{}},
              {{ status: "shutdown_scheduled", outcome_unknown: true, message: "state cannot be verified" }},
              {{ status: "ok", removed_count: 4 }},
            ];
            const messages = [];
            let refreshes = 0;
            function $$() {{ return buttons; }}
            function invalidateWechatProbeOverlay() {{}}
            function invalidateDriverProbeOverlay() {{}}
            function setStatusMessage(message) {{ messages.push(message); }}
            async function refresh(options) {{
              assert.deepEqual(options, {{ force: true }});
              refreshes += 1;
              return {{ status: "ok" }};
            }}
            async function api() {{
              const response = responses.shift();
              if (response instanceof Error) throw response;
              return response;
            }}
            {active_task}
            {sync_lock}
            {sync_locks}
            {task_failed}
            {clear_history}
            (async () => {{
              for (const expectedStatus of ["", "blocked", "failed", "conflict", "", "shutdown_scheduled"]) {{
                state.historyResetPending = false;
                syncTaskButtonLocks();
                assert.deepEqual(buttons.map((button) => button.disabled), [false, false]);
                const result = await clearHistoryData();
                assert.equal(result.status, "error");
                assert.equal(result.outcome, "unknown");
                assert.equal(result.response_status, expectedStatus);
                assert.equal(taskResultFailed(result), true);
                assert.equal(state.historyResetPending, true);
                assert.deepEqual(buttons.map((button) => button.disabled), [true, true]);
                assert.match(result.message, /请勿重复清空/);
                assert.match(result.message, /手动关闭并重新打开 sidebar/);
              }}
              assert.equal(refreshes, 0);
              state.historyResetPending = false;
              syncTaskButtonLocks();
              const ok = await clearHistoryData();
              assert.equal(ok.status, "ok");
              assert.equal(state.historyResetPending, false);
              assert.deepEqual(buttons.map((button) => button.disabled), [false, false]);
              assert.equal(refreshes, 1);
              assert.match(messages.at(-1), /历史数据已清空/);
            }})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
            """
        )

        completed = _run_node(script)

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
