"""Inline local dashboard for the control plane (no external assets)."""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="greatsage-token" content="__GREATSAGE_TOKEN__">
  <title>Great Sage Control Plane</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0c1016;
      --panel: #141b24;
      --panel-2: #1b2531;
      --line: #2a3847;
      --text: #d9e2ec;
      --muted: #8fa3b8;
      --accent: #64d2a4;
      --warn: #f0b35b;
      --danger: #f06a6a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 "Segoe UI", system-ui, sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(120deg, #111a24, #0c1016);
    }
    header h1 { font-size: 19px; margin: 0; letter-spacing: .02em; }
    header .sub { color: var(--muted); font-size: 12px; }
    .badge {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--line);
      background: var(--panel-2);
    }
    .badge.healthy { color: var(--accent); border-color: #2f6b55; }
    .badge.degraded { color: var(--warn); border-color: #6b532f; }
    .badge.unhealthy, .badge.unknown { color: var(--danger); border-color: #6b2f2f; }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(300px, .8fr);
      gap: 18px;
      padding: 18px 24px 40px;
      max-width: 1400px;
      margin: 0 auto;
    }
    .stack { display: grid; gap: 18px; align-content: start; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 16px;
    }
    .panel h2 {
      margin: 0 0 12px;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
    }
    label { display: block; color: var(--muted); font-size: 12px; margin: 10px 0 4px; }
    input, textarea {
      width: 100%;
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      font: inherit;
    }
    textarea { min-height: 92px; resize: vertical; }
    .row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .actions { display: flex; gap: 10px; margin-top: 12px; align-items: center; }
    button {
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px 13px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: var(--muted); }
    button.primary { background: #1d5c47; border-color: #2f8a6a; color: #eafff5; }
    button.danger { background: #5c2424; border-color: #8a3b3b; color: #ffecec; }
    button:disabled { opacity: .45; cursor: default; }
    .muted { color: var(--muted); }
    .mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }
    .component {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 7px 0;
      border-bottom: 1px solid var(--line);
    }
    .component:last-child { border-bottom: 0; }
    .approval {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 10px;
      background: var(--panel-2);
    }
    .approval .head { display: flex; justify-content: space-between; gap: 10px; }
    .risk { color: var(--warn); font-size: 12px; font-weight: 700; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #0e151d;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px;
      margin: 8px 0;
      max-height: 190px;
      overflow: auto;
    }
    .empty { color: var(--muted); padding: 6px 0; }
    .error { color: var(--danger); white-space: pre-wrap; }
    .item {
      border-bottom: 1px solid var(--line);
      padding: 9px 0;
      cursor: pointer;
    }
    .item:last-child { border-bottom: 0; }
    .item:hover { background: rgba(255,255,255,.025); }
    .item-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .detail {
      border-top: 1px solid var(--line);
      margin-top: 12px;
      padding-top: 12px;
    }
    .step {
      border-left: 2px solid var(--line);
      padding: 7px 0 7px 10px;
      margin: 7px 0;
    }
    .step.ok { border-left-color: #2f8a6a; }
    .step.bad { border-left-color: #8a3b3b; }
    .step .meta { color: var(--muted); font-size: 12px; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Great Sage <span class="muted">/ control plane</span></h1>
      <div class="sub">Local-only. Every action still passes the tool security pipeline.</div>
    </div>
    <div style="margin-left:auto; text-align:right;">
      <span id="overall" class="badge unknown">checking</span>
      <div id="version" class="sub"></div>
    </div>
  </header>
  <main>
    <div class="stack">
      <section class="panel">
        <h2>Agent run</h2>
        <form id="run-form">
          <label for="prompt">Prompt</label>
          <textarea id="prompt" placeholder="Ask Great Sage to inspect a workspace..."></textarea>
          <div class="row">
            <div>
              <label for="session">Session id</label>
              <input id="session" placeholder="optional">
            </div>
            <div>
              <label for="provider">Provider</label>
              <input id="provider" placeholder="auto">
            </div>
            <div>
              <label for="model">Model</label>
              <input id="model" placeholder="auto">
            </div>
          </div>
          <div class="row">
            <div>
              <label for="steps">Max steps (1-25)</label>
              <input id="steps" type="number" min="1" max="25" placeholder="configured">
            </div>
          </div>
          <div class="actions">
            <button id="run" class="primary" type="submit">Run agent</button>
            <button id="cancel" class="danger" type="button" disabled>Cancel run</button>
            <span id="run-error" class="error"></span>
          </div>
        </form>
      </section>

      <section class="panel">
        <h2>Plans</h2>
        <div id="plans" class="empty">No plans.</div>
        <div id="plan-detail"></div>
      </section>

      <section class="panel">
        <h2>Tasks</h2>
        <div id="tasks" class="empty">No tasks.</div>
        <div id="task-detail"></div>
      </section>

      <section class="panel">
        <h2>Pending approvals</h2>
        <div id="approvals" class="empty">No pending approvals.</div>
      </section>

      <section class="panel">
        <h2>Last execution</h2>
        <div id="result" class="empty">No execution yet.</div>
      </section>
    </div>

    <div class="stack">
      <section class="panel">
        <h2>Run state</h2>
        <div id="run-state" class="muted">idle</div>
      </section>
      <section class="panel">
        <h2>Components</h2>
        <div id="components" class="empty">Loading...</div>
      </section>
      <section class="panel">
        <h2>Safety</h2>
        <div class="muted">
          The dashboard answers only currently pending approvals, starts bounded
          agent runs, and runs approved READY plans through TaskService. It
          cannot execute tools directly, approve itself, or reach beyond
          loopback. Critical tools remain denied.
        </div>
      </section>
    </div>
  </main>
  <script>
    const token = document.querySelector('meta[name="greatsage-token"]').content;
    const $ = (id) => document.getElementById(id);
    let refreshing = false;
    let openPlanId = null;
    let openTaskId = null;
    let runActive = false;

    async function api(path, options = {}) {
      const headers = Object.assign(
        {"X-GreatSage-Token": token, "Content-Type": "application/json"},
        options.headers || {}
      );
      const response = await fetch(path, Object.assign({}, options, {headers}));
      let body = null;
      try { body = await response.json(); } catch (_) {}
      if (!response.ok) {
        const detail = body && (body.error || body.detail)
          ? (body.error || body.detail) : response.statusText;
        throw new Error(detail || ("HTTP " + response.status));
      }
      return body;
    }

    function text(tag, value, className) {
      const node = document.createElement(tag);
      if (value !== undefined && value !== null) node.textContent = String(value);
      if (className) node.className = className;
      return node;
    }

    function badgeClass(status) {
      const value = String(status || "").toLowerCase();
      if (["ready", "completed", "healthy"].includes(value)) return "healthy";
      if (["draft", "paused", "pending", "running", "degraded"].includes(value)) {
        return "degraded";
      }
      return "unhealthy";
    }

    function renderPlans(items) {
      const root = $("plans");
      root.replaceChildren();
      if (!items || items.length === 0) {
        root.className = "empty";
        root.textContent = "No plans.";
        return;
      }
      root.className = "";
      for (const plan of items) {
        const item = text("div", null, "item");
        const head = text("div", null, "item-head");
        head.appendChild(text("strong", plan.goal || plan.id));
        head.appendChild(
          text("span", plan.status || "-", "badge " + badgeClass(plan.status))
        );
        item.appendChild(head);
        item.appendChild(
          text("div", (plan.step_count || 0) + " steps | " + plan.id, "muted")
        );
        item.addEventListener("click", () => showPlan(plan.id));
        root.appendChild(item);
      }
    }

    async function showPlan(id) {
      if (openPlanId !== id) {
        openPlanId = id;
        const loading = $("plan-detail");
        loading.replaceChildren();
        loading.className = "detail";
        loading.appendChild(text("div", "Loading plan...", "muted"));
      }
      try {
        const data = await api("/api/plans/" + encodeURIComponent(id));
        if (openPlanId !== id) return;
        renderPlanDetail(data.plan || {});
      } catch (err) {
        if (openPlanId === id) {
          $("plan-detail").replaceChildren(text("div", err.message, "error"));
        }
      }
    }

    function renderPlanDetail(plan) {
      const root = $("plan-detail");
      root.replaceChildren();
      root.className = "detail";
      root.appendChild(text("div", plan.goal || plan.id, ""));
      root.appendChild(
        text(
          "div",
          "status " + (plan.status || "-") + " | " +
            ((plan.steps || []).length) + " step(s)",
          "muted"
        )
      );
      if (plan.workspace_root) {
        root.appendChild(text("div", "workspace " + plan.workspace_root, "muted"));
      }
      for (const step of plan.steps || []) {
        const card = text("div", null, "step");
        card.appendChild(
          text(
            "div",
            "#" + step.sequence + " " + (step.description || step.tool_id)
          )
        );
        card.appendChild(
          text(
            "div",
            (step.tool_id || "-") + " | risk " + (step.risk_estimate || "-"),
            "meta"
          )
        );
        if (step.depends_on && step.depends_on.length) {
          card.appendChild(
            text("div", "depends on " + step.depends_on.join(", "), "meta")
          );
        }
        if (step.arguments_preview) {
          card.appendChild(text("pre", step.arguments_preview, "mono"));
        }
        root.appendChild(card);
      }
      const actions = text("div", null, "actions");
      if (plan.status === "draft") {
        const approve = text("button", "Approve plan", "primary");
        approve.addEventListener("click", () => approvePlan(plan.id));
        actions.appendChild(approve);
      } else if (plan.status === "ready") {
        const run = text("button", "Approve and run", "primary");
        run.disabled = runActive;
        run.addEventListener("click", () => runPlan(plan.id));
        actions.appendChild(run);
      }
      root.appendChild(actions);
    }

    async function approvePlan(id) {
      try {
        await api(
          "/api/plans/" + encodeURIComponent(id) + "/approve",
          {method: "POST", body: JSON.stringify({approved: true})}
        );
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    }

    async function runPlan(id) {
      if (runActive) return;
      if (!confirm("Run this READY plan through TaskService?")) return;
      try {
        await api(
          "/api/plans/" + encodeURIComponent(id) + "/run",
          {method: "POST", body: JSON.stringify({approved: true})}
        );
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    }

    function renderTasks(items) {
      const root = $("tasks");
      root.replaceChildren();
      if (!items || items.length === 0) {
        root.className = "empty";
        root.textContent = "No tasks.";
        return;
      }
      root.className = "";
      for (const task of items) {
        const item = text("div", null, "item");
        const head = text("div", null, "item-head");
        head.appendChild(text("strong", task.goal || task.id));
        head.appendChild(
          text("span", task.state || "-", "badge " + badgeClass(task.state))
        );
        item.appendChild(head);
        item.appendChild(
          text(
            "div",
            (task.completed_steps || 0) + "/" + (task.total_steps || 0) +
              " steps | " + task.id,
            "muted"
          )
        );
        item.addEventListener("click", () => showTask(task.id));
        root.appendChild(item);
      }
    }

    async function showTask(id) {
      if (openTaskId !== id) {
        openTaskId = id;
        const loading = $("task-detail");
        loading.replaceChildren();
        loading.className = "detail";
        loading.appendChild(text("div", "Loading task...", "muted"));
      }
      try {
        const data = await api("/api/tasks/" + encodeURIComponent(id));
        if (openTaskId !== id) return;
        renderTaskDetail(data.task || {});
      } catch (err) {
        if (openTaskId === id) {
          $("task-detail").replaceChildren(text("div", err.message, "error"));
        }
      }
    }

    function renderTaskDetail(task) {
      const root = $("task-detail");
      root.replaceChildren();
      root.className = "detail";
      root.appendChild(text("div", task.goal || task.id, ""));
      root.appendChild(
        text(
          "div",
          "state " + (task.state || "-") + " | plan " + (task.plan_id || "-"),
          "muted"
        )
      );
      root.appendChild(
        text(
          "div",
          "progress " + (task.completed_steps || 0) + "/" +
            (task.total_steps || 0) + " | failed " + (task.failed_steps || 0),
          "muted"
        )
      );
      if (task.error) root.appendChild(text("div", task.error, "error"));
      for (const result of task.step_results || []) {
        const card = text("div", null, "step " + (result.success ? "ok" : "bad"));
        card.appendChild(
          text(
            "div",
            "#" + result.sequence + " " + (result.tool_id || "-") +
              " | " + (result.success ? "ok" : "failed")
          )
        );
        if (result.error) card.appendChild(text("div", result.error, "error"));
        root.appendChild(card);
      }
      const actions = text("div", null, "actions");
      if (task.plan_id) {
        const view = text("button", "View plan", "");
        view.addEventListener("click", () => showPlan(task.plan_id));
        actions.appendChild(view);
      }
      if (["failed", "timed_out", "paused"].includes(task.state)) {
        const resume = text("button", "Approve and resume", "primary");
        resume.disabled = runActive;
        resume.addEventListener("click", () => resumeTask(task.id));
        actions.appendChild(resume);
      }
      if (["pending", "running", "paused"].includes(task.state)) {
        const cancel = text("button", "Cancel task", "danger");
        cancel.addEventListener("click", () => cancelTask(task.id));
        actions.appendChild(cancel);
      }
      root.appendChild(actions);
    }

    async function resumeTask(id) {
      if (runActive) return;
      if (!confirm("Resume this task with explicit approval?")) return;
      try {
        await api(
          "/api/tasks/" + encodeURIComponent(id) + "/resume",
          {method: "POST", body: JSON.stringify({approved: true})}
        );
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    }

    async function cancelTask(id) {
      try {
        await api(
          "/api/tasks/" + encodeURIComponent(id) + "/cancel",
          {method: "POST", body: "{}"}
        );
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    }

    function renderApprovals(items) {
      const root = $("approvals");
      root.replaceChildren();
      if (!items || items.length === 0) {
        root.className = "empty";
        root.textContent = "No pending approvals.";
        return;
      }
      root.className = "";
      for (const item of items) {
        const card = text("div", null, "approval");
        const head = text("div", null, "head");
        const title = document.createElement("div");
        title.appendChild(text("strong", item.tool_id));
        title.appendChild(text("span", "  " + (item.tool_name || ""), "muted"));
        head.appendChild(title);
        head.appendChild(text("span", item.risk_level, "risk"));
        card.appendChild(head);
        card.appendChild(text("div", item.reason || "approval required", "muted"));
        card.appendChild(text("pre", item.arguments_preview || "(no arguments)", "mono"));
        const actions = text("div", null, "actions");
        const approve = text("button", "Approve", "primary");
        const reject = text("button", "Reject", "danger");
        approve.addEventListener("click", () => decide(item.request_id, true));
        reject.addEventListener("click", () => decide(item.request_id, false));
        actions.appendChild(approve);
        actions.appendChild(reject);
        card.appendChild(actions);
        root.appendChild(card);
      }
    }

    async function decide(requestId, approve) {
      try {
        const decision = approve ? "approve" : "reject";
        const path = "/api/approvals/" + encodeURIComponent(requestId) + "/" + decision;
        await api(path, {method: "POST"});
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    }

    function renderResult(result, kind) {
      const root = $("result");
      root.replaceChildren();
      if (!result) {
        root.className = "empty";
        root.textContent = "No execution yet.";
        return;
      }
      root.className = "";
      const state = text("div", null, "component");
      state.appendChild(text("strong", "State"));
      state.appendChild(text("span", result.state || "-"));
      root.appendChild(state);
      const keys = kind === "task"
        ? ["task_id", "plan_id", "goal", "total_steps", "completed_steps",
           "failed_steps", "duration_ms", "summary"]
        : ["task_id", "provider", "model", "tool_calls", "duration_ms"];
      for (const key of keys) {
        if (result[key] === undefined || result[key] === null) continue;
        const row = text("div", null, "component");
        row.appendChild(text("span", key, "muted"));
        const value = key === "duration_ms" ? Math.round(result[key]) + " ms" : result[key];
        row.appendChild(text("span", value));
        root.appendChild(row);
      }
      for (const step of result.step_results || []) {
        const card = text("div", null, "step " + (step.success ? "ok" : "bad"));
        card.appendChild(
          text(
            "div",
            "#" + step.sequence + " " + (step.tool_id || "-") +
              " | " + (step.success ? "ok" : "failed")
          )
        );
        if (step.error) card.appendChild(text("div", step.error, "error"));
        root.appendChild(card);
      }
      if (result.final_text) root.appendChild(text("pre", result.final_text, "mono"));
      if (result.error) root.appendChild(text("div", result.error, "error"));
    }

    function render(state) {
      const status = state.status || {};
      const overall = (status.overall || "unknown").toUpperCase();
      const badge = $("overall");
      badge.textContent = overall;
      badge.className = "badge " + overall.toLowerCase();
      $("version").textContent = status.version ? ("v" + status.version) : "";
      const components = $("components");
      components.replaceChildren();
      if (!status.components || status.components.length === 0) {
        components.className = "empty";
        components.textContent = status.health_error || "No component health available.";
      } else {
        components.className = "";
        for (const item of status.components) {
          const row = text("div", null, "component");
          row.appendChild(text("span", item.component));
          row.appendChild(text("span", item.status, "badge " + String(item.status).toLowerCase()));
          components.appendChild(row);
        }
      }
      const run = state.run || {};
      runActive = !!run.active;
      $("run").disabled = runActive;
      $("cancel").disabled = !runActive;
      const current = (state.agent && state.agent.current) || {};
      if (run.active && run.kind === "task") {
        $("run-state").textContent = "running task | plan " +
          (run.plan_id || "-") + " | task " + (run.task_id || "-");
      } else if (run.active) {
        $("run-state").textContent = "running · " + (current.state || "starting") +
          " · steps " + (current.steps || 0) + " · tools " + (current.tool_calls || 0);
      } else if (run.result) {
        $("run-state").textContent = "finished · " + (run.result.state || "unknown");
      } else if (run.error) {
        $("run-state").textContent = "failed · " + run.error;
      } else {
        $("run-state").textContent = "idle";
      }
      renderPlans(state.plans || []);
      renderTasks(state.tasks || []);
      renderApprovals(run.approvals || []);
      renderResult(run.result, run.kind);
      if (run.error) $("run-error").textContent = run.error;
      if (openPlanId) showPlan(openPlanId);
      if (openTaskId) showTask(openTaskId);
    }

    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      try {
        render(await api("/api/state"));
      } catch (err) {
        $("run-error").textContent = err.message;
      } finally {
        refreshing = false;
      }
    }

    $("run-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      $("run-error").textContent = "";
      const prompt = $("prompt").value.trim();
      if (!prompt) {
        $("run-error").textContent = "Prompt must not be empty.";
        return;
      }
      const payload = {prompt};
      const fields = [
        ["session_id", "session"],
        ["provider", "provider"],
        ["model", "model"],
      ];
      for (const [key, id] of fields) {
        const value = $(id).value.trim();
        if (value) payload[key] = value;
      }
      const steps = $("steps").value.trim();
      if (steps) payload.max_steps = Number(steps);
      $("run").disabled = true;
      try {
        await api("/api/agent", {method: "POST", body: JSON.stringify(payload)});
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
        $("run").disabled = false;
      }
    });

    $("cancel").addEventListener("click", async () => {
      try {
        await api("/api/run/cancel", {method: "POST", body: "{}"});
        await refresh();
      } catch (err) {
        $("run-error").textContent = err.message;
      }
    });

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""
