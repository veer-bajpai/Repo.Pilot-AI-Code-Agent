/* RepoPilot console. No framework, no build step.
 * Security notes:
 *  - Everything from the server or the repository is rendered with textContent, never innerHTML.
 *  - There are no visitor API keys. The Gemini key lives only on the server and is never sent here.
 *  - The optional GitHub token (for "Push and open PR") is used for one request and never stored.
 *  - The browser holds an anonymous, HttpOnly cookie set by the server; it scopes your repos and runs.
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TERMINAL = new Set([
    "completed",
    "failed",
    "cancelled",
    "budget_exceeded",
    "interrupted",
  ]);

  const state = {
    config: null,
    ai: { available: false, checked: false, reason: "" },
    sessions: [],
    sessionId: null,
    runId: null,
    lastEvent: 0,
    timer: null,
    lastTool: null,
    gates: {},
    pending: new Set(),
    usage: null,
    run: null,
    ended: false,
  };

  function setMascotMood(mood, text) {
    const avatar = $("agentAvatar");
    const bubble = $("agentBubble");
    if (!avatar || !bubble) return;
    avatar.className = "agent-avatar mood-" + mood;
    bubble.textContent = text || "Ready to inspect code";
  }

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const clear = (n) => {
    while (n.firstChild) n.removeChild(n.firstChild);
    return n;
  };
  const show = (n, on = true) => {
    n.hidden = !on;
  };
  const fmtCost = (c) => "$" + (c || 0).toFixed(c && c < 0.1 ? 4 : 2);
  function toast(msg, ms = 3500) {
    const t = $("toast");
    t.textContent = msg;
    show(t);
    clearTimeout(toast.t);
    toast.t = setTimeout(() => show(t, false), ms);
  }
  function openDialog(d) {
    if (!d.open) {
      d.showModal ? d.showModal() : d.setAttribute("open", "");
    }
  }
  function closeDialog(d) {
    if (d.open) {
      d.close ? d.close() : d.removeAttribute("open");
    }
  }

  // ------------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (opts.githubToken) headers["X-GitHub-Token"] = opts.githubToken;
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || "GET",
        headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
    } catch (_) {
      throw Object.assign(
        new Error("Cannot reach the RepoPilot server. Is it still running?"),
        { status: 0 },
      );
    }
    if (res.status === 204) return null;
    if (opts.raw) {
      if (!res.ok)
        throw Object.assign(
          new Error((await safeJson(res)).detail || res.statusText),
          { status: res.status },
        );
      return res;
    }
    const data = await safeJson(res);
    if (!res.ok) {
      let msg = data.detail;
      if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join("; ");
      throw Object.assign(
        new Error(msg || res.statusText || "Request failed"),
        { status: res.status, data },
      );
    }
    return data;
  }
  async function safeJson(res) {
    try {
      return await res.json();
    } catch (_) {
      return {};
    }
  }

  // ------------------------------------------------------------------ AI status
  const aiOk = () =>
    !!(state.config && state.config.ai_available && state.ai.available);
  function renderAi() {
    const pill = $("aiPill"),
      c = state.config;
    if (!c.ai_available) {
      pill.className = "pill pill-muted";
      pill.textContent = "Demo mode \u00b7 live AI off";
    } else if (!state.ai.checked) {
      pill.className = "pill pill-muted";
      pill.textContent = "Checking AI\u2026";
    } else if (state.ai.available) {
      pill.className = "pill pill-ok";
      pill.textContent = c.provider + " \u00b7 " + c.model;
    } else {
      pill.className = "pill pill-bad";
      pill.textContent = "Live AI unavailable";
    }
    pill.title = state.ai.reason || "";
    renderComposer();
  }
  async function loadAi() {
    if (!state.config.ai_available) {
      state.ai = {
        available: false,
        checked: true,
        reason:
          "Live AI is not configured on this server. The built-in demos still work.",
      };
      renderAi();
      return;
    }
    try {
      const r = await api("/api/ai/status");
      state.ai = {
        available: !!r.available,
        checked: true,
        reason: r.reason || "",
      };
    } catch (ex) {
      state.ai = { available: false, checked: true, reason: ex.message };
    }
    renderAi();
  }

  // ------------------------------------------------------------------ sessions
  const current = () =>
    state.sessions.find((s) => s.id === state.sessionId) || null;
  async function refreshSessions() {
    state.sessions = await api("/api/sessions");
    renderSessions();
    if (state.sessions.some((s) => s.status === "cloning")) {
      clearTimeout(refreshSessions.t);
      refreshSessions.t = setTimeout(
        () => refreshSessions().catch(() => {}),
        1500,
      );
    }
    const cur = current();
    if (cur) renderWorkspaceHead(cur);
  }
  function renderSessions() {
    const ul = clear($("sessionList"));
    show($("sessionEmpty"), state.sessions.length === 0);
    for (const s of state.sessions) {
      const stateText =
        s.status === "ready"
          ? s.kind === "demo"
            ? "Demo, ready"
            : "Ready"
          : s.status === "cloning"
            ? "Cloning\u2026"
            : s.error || "Failed";
      ul.append(
        el(
          "li",
          { class: "session-item" },
          el(
            "button",
            {
              class: "session-open",
              type: "button",
              "aria-current": s.id === state.sessionId ? "true" : "false",
              onclick: () => selectSession(s.id),
            },
            el("span", { class: "name", text: s.name }),
            el("span", {
              class: "state" + (s.status === "error" ? " bad" : ""),
              text: stateText,
            }),
          ),
          el(
            "button",
            {
              class: "session-del",
              type: "button",
              "aria-label": "Remove " + s.name,
              title: "Remove",
              onclick: () => removeSession(s),
            },
            "\u00d7",
          ),
        ),
      );
    }
  }
  async function removeSession(s) {
    if (!confirm("Remove " + s.name + " and its run history?")) return;
    try {
      await api("/api/sessions/" + s.id, { method: "DELETE" });
    } catch (ex) {
      toast(ex.message);
      return;
    }
    if (state.sessionId === s.id) {
      stopPolling();
      state.sessionId = null;
      state.runId = null;
      show($("workspace"), false);
      show($("welcome"));
    }
    await refreshSessions();
  }
  async function addSession(body, errBox) {
    show(errBox, false);
    try {
      const s = await api("/api/sessions", { method: "POST", body });
      await refreshSessions();
      await selectSession(s.id);
      return s;
    } catch (ex) {
      errBox.textContent = ex.message;
      show(errBox);
      return null;
    }
  }
  async function selectSession(id) {
    stopPolling();
    state.sessionId = id;
    state.runId = null;
    renderSessions();
    show($("welcome"), false);
    show($("workspace"));
    const s = current();
    if (!s) return;
    renderWorkspaceHead(s);
    resetRunView();
    renderComposer();
    if (s.kind === "demo") {
      const demo = state.config.demos.find(
        (d) => d.id === (s.analysis && s.analysis.demo_id),
      );
      if (demo && !$("task").value.trim()) $("task").value = demo.task;
    }
    await loadPastRuns();
  }
  function renderWorkspaceHead(s) {
    $("wsName").textContent = s.name;
    const a = s.analysis || {};
    const parts = [];
    if (s.status === "cloning") parts.push("Cloning\u2026");
    if (s.status === "error") parts.push(s.error || "Failed to load");
    if (a.files != null) parts.push(a.files + " files");
    if (a.languages && a.languages.length)
      parts.push(a.languages.map((l) => l.language).join(", "));
    if (s.status === "ready") {
      const testsOn = !state.config.public_mode || s.kind === "demo";
      parts.push(
        !testsOn
          ? "Tests disabled on this server"
          : a.test_command
            ? "Tests: " + a.test_command
            : "No test command detected",
      );
    }
    $("wsMeta").textContent = parts.join(" \u00b7 ");
  }
  function renderComposer() {
    const s = current();
    if (!s) return;
    const isDemo = s.kind === "demo";
    show($("engineSeg"), isDemo);
    const live = document.querySelector("input[name=engine][value=live]");
    live.disabled = !aiOk();
    if (!aiOk())
      document.querySelector("input[name=engine][value=simulated]").checked =
        true;
    const active = state.run && !TERMINAL.has(state.run.status) && state.runId;
    const needsAi = !isDemo && !aiOk();
    $("runBtn").disabled =
      s.status !== "ready" || !!active || (needsAi && state.ai.checked);
    const hint = $("aiHint");
    hint.textContent =
      needsAi && state.ai.checked
        ? state.ai.reason ||
          "Live AI is unavailable, so only the built-in demos can run right now."
        : "";
    show(hint, !!hint.textContent);
    show($("cancelBtn"), !!active);

    if (!state.runId) {
      setMascotMood("idle", "Ready to inspect code");
    }
  }

  // ------------------------------------------------------------------ run lifecycle
  function resetRunView() {
    state.lastTool = null;
    state.gates = {};
    state.pending = new Set();
    state.usage = null;
    state.run = null;
    state.ended = false;
    state.lastEvent = 0;
    clear($("timeline"));
    clear($("meters"));
    clear($("result"));
    show($("result"), false);
    show($("runPanel"), false);
    show($("approvalNotice"), false);
    document.title = "RepoPilot";
    show($("runError"), false);
  }
  async function startRun() {
    const s = current();
    if (!s) return;
    const task = $("task").value.trim();
    const err = $("runError");
    show(err, false);
    if (task.length < 5) {
      err.textContent = "Describe the task in a sentence or two.";
      show(err);
      return;
    }
    const isDemo = s.kind === "demo";
    const engine = isDemo
      ? document.querySelector("input[name=engine]:checked").value
      : "live";
    if (engine === "live" && !aiOk()) {
      err.textContent = state.ai.reason || "Live AI is unavailable right now.";
      show(err);
      return;
    }
    const body = {
      task,
      approval_mode: document.querySelector("input[name=mode]:checked").value,
      mode: engine,
    };
    $("runBtn").disabled = true;
    try {
      const run = await api("/api/sessions/" + s.id + "/runs", {
        method: "POST",
        body,
      });
      resetRunView();
      state.runId = run.id;
      state.run = run;
      show($("runPanel"));
      renderMeters();
      renderComposer();
      startPolling();
    } catch (ex) {
      err.textContent = ex.message;
      show(err);
      renderComposer();
    }
  }
  async function cancelRun() {
    try {
      await api("/api/runs/" + state.runId + "/cancel", { method: "POST" });
      toast("Cancelling\u2026");
    } catch (ex) {
      toast(ex.message);
    }
  }
  function startPolling() {
    stopPolling();
    tick();
    state.timer = setInterval(tick, 900);
  }
  function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }
  async function tick() {
    if (!state.runId || tick.busy) return;
    tick.busy = true;
    try {
      const data = await api(
        "/api/runs/" + state.runId + "/events?after=" + state.lastEvent,
      );
      state.run = data.run;
      for (const ev of data.events) {
        state.lastEvent = ev.id;
        handleEvent(ev);
      }
      renderMeters();
      renderComposer();
      updateNotice();
      if (
        TERMINAL.has(data.run.status) &&
        !state.ended &&
        data.events.length === 0
      ) {
        state.ended = true;
        stopPolling();
        await showResult();
        await loadPastRuns();
      }
    } catch (ex) {
      if (ex.status === 404) stopPolling();
      else showBanner(ex.message, true);
    } finally {
      tick.busy = false;
    }
  }
  function showBanner(msg, transient) {
    const b = $("banner");
    b.textContent = msg;
    show(b);
    if (transient) {
      clearTimeout(showBanner.t);
      showBanner.t = setTimeout(() => show(b, false), 5000);
    }
  }
  function updateNotice() {
    const n = state.pending.size;
    show($("approvalNotice"), n > 0);
    document.title =
      n > 0 ? "(" + n + ") Approval needed \u00b7 RepoPilot" : "RepoPilot";
  }

  // ------------------------------------------------------------------ meters
  function meter(label, value, ratio) {
    const m = el(
      "div",
      { class: "meter" },
      el("b", { text: value }),
      el("span", { text: label }),
    );
    if (ratio != null)
      m.append(
        el(
          "div",
          { class: "bar" },
          el("i", {
            style: "width:" + Math.min(100, Math.round(ratio * 100)) + "%",
          }),
        ),
      );
    return m;
  }
  function statusTag(status) {
    const map = {
      running: ["Running", "tag-neutral"],
      awaiting_approval: ["Waiting for you", "tag-warn"],
      completed: ["Complete", "tag-ok"],
      failed: ["Failed", "tag-bad"],
      cancelled: ["Cancelled", "tag-neutral"],
      budget_exceeded: ["Budget reached", "tag-warn"],
      interrupted: ["Interrupted", "tag-bad"],
    };
    const [t, c] = map[status] || [status, "tag-neutral"];
    return el("span", { class: "tag " + c, text: t });
  }
  function renderMeters() {
    const box = clear($("meters"));
    const r = state.run;
    if (!r) return;
    const L = state.config.limits,
      u = state.usage || {
        steps: r.steps,
        cost_usd: r.cost_usd,
        files_changed: r.files_changed,
        lines_changed: r.lines_changed,
        tokens_in: r.tokens_in,
        tokens_out: r.tokens_out,
      };
    const sim = r.llm_kind === "simulated";
    const st = el(
      "div",
      { class: "meter status" },
      statusTag(r.status),
      el("span", {
        text: sim ? "Simulated (no model called)" : r.model || "Live AI",
      }),
    );
    box.append(
      st,
      meter("steps", u.steps + " / " + L.max_steps, u.steps / L.max_steps),
      meter(
        "est. cost",
        fmtCost(u.cost_usd) + " / " + fmtCost(L.max_cost_usd),
        u.cost_usd / L.max_cost_usd,
      ),
      meter(
        "files changed",
        u.files_changed + " / " + L.max_files_changed,
        u.files_changed / L.max_files_changed,
      ),
      meter(
        "lines changed",
        u.lines_changed + " / " + L.max_changed_lines,
        u.lines_changed / L.max_changed_lines,
      ),
      meter(
        "tokens in / out",
        (u.tokens_in || 0).toLocaleString() +
          " / " +
          (u.tokens_out || 0).toLocaleString(),
      ),
    );
  }

  // ------------------------------------------------------------------ timeline
  function describe(tool, a) {
    a = a || {};
    switch (tool) {
      case "search_code":
        return ["Searched the code", '"' + (a.query || "") + '"'];
      case "read_file":
        return [
          "Read",
          (a.path || "") +
            (a.start_line
              ? ":" + a.start_line + (a.end_line ? "-" + a.end_line : "")
              : ""),
        ];
      case "list_files":
        return ["Listed files", a.path || "."];
      case "replace_in_file":
        return ["Edit", a.path || ""];
      case "write_file":
        return ["Write", a.path || ""];
      case "run_tests":
        return ["Run tests", a.command || "default command"];
      case "git_diff":
        return ["Checked the diff so far", ""];
      case "update_plan":
        return ["Plan", ""];
      case "finish":
        return ["Finished", ""];
      default:
        return [tool, ""];
    }
  }
  function addItem(cls, ...kids) {
    const li = el("li", { class: "tl-item " + (cls || "") }, ...kids);
    $("timeline").append(li);
    return li;
  }
  function renderDiff(text) {
    const box = el("div", {
      class: "diff",
      role: "region",
      "aria-label": "Diff",
      tabindex: "0",
    });
    for (const line of (text || "").split("\n")) {
      let c = "ln";
      if (
        line.startsWith("diff --git") ||
        line.startsWith("+++ ") ||
        line.startsWith("--- ") ||
        line.startsWith("index ")
      )
        c += " file";
      else if (line.startsWith("@@")) c += " hunk";
      else if (line.startsWith("+")) c += " add";
      else if (line.startsWith("-")) c += " del";
      box.append(el("span", { class: c, text: line || " " }));
    }
    return box;
  }
  function handleEvent(ev) {
    const d = ev.data || {};
    switch (ev.kind) {
      case "run_started":
        setMascotMood("thinking", "Planning the next fix");
        addItem(
          "",
          el(
            "div",
            { class: "tl-title" },
            "Run started",
            el("span", {
              class: "tag " + (d.simulated ? "tag-sim" : "tag-live"),
              text: d.simulated
                ? "Simulated: no model called"
                : "Live: " + (d.model || "model"),
            }),
            el("span", {
              class: "tag tag-neutral",
              text:
                d.mode === "auto"
                  ? "Auto-approve confident edits"
                  : "Ask every time",
            }),
          ),
          d.tests_enabled
            ? null
            : el("div", {
                class: "tl-sub",
                text: "Test execution is disabled on this server for this repository.",
              }),
        );
        break;
      case "usage":
        state.usage = d;
        break;
      case "message":
        if (d.text && /approve|approval|waiting/i.test(d.text))
          setMascotMood("alert", "Needs your approval");
        else if (d.text && /test|run|complete|done/i.test(d.text))
          setMascotMood("success", "Nice progress");
        addItem("msg", el("div", { text: d.text }));
        break;
      case "tool_call": {
        const [title, code] = describe(d.tool, d.args);
        state.lastTool = addItem(
          d.tool === "finish" ? "ok" : "",
          el(
            "div",
            { class: "tl-title" },
            title,
            code ? el("code", { text: code }) : null,
          ),
        );
        state.lastTool.dataset.tool = d.tool;
        if (d.tool === "replace_in_file" && d.args && d.args.reason)
          state.lastTool.append(
            el("div", { class: "tl-sub", text: d.args.reason }),
          );
        setMascotMood(
          "working",
          d.tool === "run_tests" ? "Testing the fix" : "Building the patch",
        );
        break;
      }
      case "plan": {
        const item = state.lastTool || addItem("");
        item.append(
          el(
            "ol",
            { class: "plan" },
            (d.steps || []).map((s) => el("li", { text: s })),
          ),
        );
        item.classList.add("ok");
        setMascotMood("thinking", "Mapping out the fix");
        break;
      }
      case "tool_result": {
        const item = state.lastTool || addItem("");
        item.classList.add(d.ok ? "ok" : "bad");
        const out = String(d.output || "");
        if (out && item.dataset.tool !== "update_plan") {
          const lines = out.split("\n").length;
          item.append(
            el(
              "details",
              {},
              el("summary", {
                text: d.ok ? "Output (" + lines + " lines)" : "Details",
              }),
              el("pre", { class: "out", text: out }),
            ),
          );
        }
        setMascotMood(
          d.ok ? "success" : "error",
          d.ok ? "Patch is looking good" : "One issue needs attention",
        );
        break;
      }
      case "refused": {
        const item = state.lastTool || addItem("");
        item.classList.add("refused");
        item.append(
          el(
            "div",
            { class: "tl-sub" },
            el("span", { class: "tag tag-bad", text: "Refused by guardrail" }),
            " ",
            d.reason,
          ),
        );
        setMascotMood("alert", "Guardrail stopped that move");
        break;
      }
      case "auto_approved": {
        const item = state.lastTool || addItem("");
        item.append(
          el(
            "div",
            { class: "tl-sub" },
            el("span", { class: "tag tag-ok", text: "Auto-approved" }),
            " ",
            d.reason,
          ),
        );
        setMascotMood("success", "Auto-approved and moving");
        break;
      }
      case "approval_requested":
        setMascotMood("alert", "Waiting on your decision");
        buildGate(d);
        break;
      case "approval_decided":
        setMascotMood(
          d.approved ? "success" : "error",
          d.approved ? "Approved — continuing" : "Rejected — revising the plan",
        );
        markGate(d.approval_id, d.approved, d.note);
        break;
      default:
        break;
    }
  }
  function buildGate(d) {
    const item = state.lastTool || addItem("");
    item.classList.add("gate");
    const isEdit = d.tool !== "run_tests";
    const note = el("input", {
      type: "text",
      placeholder: "Optional note to the agent",
      maxlength: "500",
      "aria-label": "Note to the agent",
    });
    const approve = el(
      "button",
      { class: "btn btn-go btn-small", type: "button" },
      "Approve",
    );
    const reject = el(
      "button",
      { class: "btn btn-stop btn-small", type: "button" },
      "Reject",
    );
    const decide = async (ok) => {
      approve.disabled = reject.disabled = true;
      try {
        await api("/api/approvals/" + d.approval_id, {
          method: "POST",
          body: { approved: ok, note: note.value },
        });
        markGate(d.approval_id, ok, note.value);
      } catch (ex) {
        toast(ex.message);
        if (ex.status !== 409) approve.disabled = reject.disabled = false;
        else markGate(d.approval_id, null, "");
      }
    };
    approve.addEventListener("click", () => decide(true));
    reject.addEventListener("click", () => decide(false));
    const head = el(
      "div",
      { class: "gate-head" },
      isEdit
        ? "Approval needed: edit " + (d.target || "")
        : "Approval needed: run " + (d.target || "tests"),
    );
    const card = el(
      "div",
      { class: "gate-card" },
      head,
      el(
        "div",
        { class: "gate-reason" },
        d.reason || "",
        d.confidence != null ? " " : null,
        d.confidence != null
          ? el("span", {
              class: "tag tag-neutral",
              text: "confidence " + d.confidence.toFixed(2),
            })
          : null,
        d.protected ? " " : null,
        d.protected
          ? el("span", { class: "tag tag-warn", text: "protected file" })
          : null,
      ),
      renderDiffOrCommand(d, isEdit),
      el("div", { class: "gate-actions" }, approve, reject, note),
    );
    item.append(card);
    state.gates[d.approval_id] = {
      card,
      head,
      actions: card.querySelector(".gate-actions"),
    };
    state.pending.add(d.approval_id);
    updateNotice();
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function renderDiffOrCommand(d, isEdit) {
    return isEdit
      ? renderDiff(d.preview)
      : el("pre", { class: "out", text: d.preview || "" });
  }
  function markGate(id, approved, note) {
    const g = state.gates[id];
    state.pending.delete(id);
    updateNotice();
    if (!g || g.card.classList.contains("decided")) return;
    g.card.classList.add("decided");
    clear(g.actions).append(
      el("span", {
        class:
          "tag " +
          (approved === true
            ? "tag-ok"
            : approved === false
              ? "tag-bad"
              : "tag-neutral"),
        text:
          approved === true
            ? "Approved"
            : approved === false
              ? "Rejected"
              : "Already decided",
      }),
      note ? el("span", { class: "tl-sub", text: " " + note }) : null,
    );
  }

  // ------------------------------------------------------------------ result
  async function showResult() {
    const rid = state.runId;
    const [run, d] = await Promise.all([
      api("/api/runs/" + rid),
      api("/api/runs/" + rid + "/diff"),
    ]);
    state.run = run;
    renderMeters();
    renderComposer();
    const s = current();
    const box = clear($("result"));
    show(box);
    const headline =
      {
        completed: "Run complete",
        failed: "Run failed",
        cancelled: "Run cancelled",
        budget_exceeded: "Stopped at a budget limit",
        interrupted: "Run interrupted",
      }[run.status] || "Run finished";
    const tests =
      run.tests_passed === 1
        ? ["Tests passing", "tag-ok"]
        : run.tests_passed === 0
          ? ["Tests failing", "tag-bad"]
          : ["Tests not run", "tag-neutral"];
    box.append(
      el("h2", { text: headline }),
      el(
        "div",
        { class: "result-grid" },
        statusTag(run.status),
        el("span", { class: "tag " + tests[1], text: tests[0] }),
        el("span", {
          class: "tag tag-neutral",
          text:
            run.files_changed +
            " file(s), " +
            run.lines_changed +
            " line(s) changed",
        }),
        run.llm_kind === "simulated"
          ? el("span", {
              class: "tag tag-sim",
              text: "Simulated: token and cost figures are estimates",
            })
          : el("span", {
              class: "tag tag-neutral",
              text: "Estimated cost " + fmtCost(run.cost_usd),
            }),
      ),
    );
    if (run.summary)
      box.append(el("p", { class: "summary", text: run.summary }));
    if (run.error) box.append(el("p", { class: "error", text: run.error }));
    if (d.files.length) {
      box.append(
        el(
          "ul",
          { class: "files-list" },
          d.files.map((f) =>
            el(
              "li",
              {},
              el("span", { class: "path", text: f.path }),
              " +" + f.added + " \u2212" + f.removed,
            ),
          ),
        ),
      );
      box.append(renderDiff(d.diff));
      const actions = el(
        "div",
        { class: "result-actions" },
        el(
          "button",
          {
            class: "btn btn-primary",
            type: "button",
            onclick: () => downloadPatch(rid),
          },
          "Download patch",
        ),
        el(
          "button",
          {
            class: "btn btn-quiet",
            type: "button",
            onclick: () => copyDiff(d.diff),
          },
          "Copy diff",
        ),
      );
      const canPR =
        s &&
        s.kind === "repo" &&
        s.analysis &&
        s.analysis.host === "github.com";
      if (canPR)
        actions.append(
          el(
            "button",
            {
              class: "btn btn-quiet",
              type: "button",
              onclick: () => openGh(run),
            },
            "Push and open PR",
          ),
        );
      box.append(actions, el("p", { class: "hint", id: "prOut" }));
    } else if (run.status === "completed") {
      box.append(
        el("p", { class: "hint", text: "The agent made no file changes." }),
      );
    }
  }
  async function downloadPatch(rid) {
    try {
      const res = await api("/api/runs/" + rid + "/patch", { raw: true });
      const blob = await res.blob();
      const a = el("a", {
        href: URL.createObjectURL(blob),
        download: "repopilot-" + rid + ".patch",
      });
      document.body.append(a);
      a.click();
      a.remove();
    } catch (ex) {
      toast(ex.message);
    }
  }
  async function copyDiff(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Diff copied.");
    } catch (_) {
      toast("Copy failed. Use Download patch instead.");
    }
  }
  function openGh(run) {
    $("ghInput").value = "";
    $("prTitle").value = (run.task || "").split("\n")[0].slice(0, 100);
    show($("ghError"), false);
    $("ghGo").disabled = false;
    $("ghGo").textContent = "Push and open PR";
    $("ghForm").dataset.run = run.id;
    openDialog($("ghDialog"));
    $("ghInput").focus();
  }
  async function submitGh(e) {
    e.preventDefault();
    const token = $("ghInput").value.trim();
    const err = $("ghError");
    show(err, false);
    if (!token) {
      err.textContent = "Enter a GitHub token.";
      show(err);
      return;
    }
    $("ghGo").disabled = true;
    $("ghGo").textContent = "Pushing\u2026";
    try {
      const out = await api("/api/runs/" + $("ghForm").dataset.run + "/pr", {
        method: "POST",
        body: { push: true, title: $("prTitle").value },
        githubToken: token,
      });
      $("ghInput").value = "";
      closeDialog($("ghDialog"));
      const p = $("prOut");
      if (p) {
        clear(p).append(
          "Pull request opened: ",
          el("a", {
            href: out.pr_url,
            target: "_blank",
            rel: "noopener noreferrer",
            text: out.pr_url,
          }),
        );
      }
    } catch (ex) {
      err.textContent = ex.message;
      show(err);
      $("ghGo").disabled = false;
      $("ghGo").textContent = "Push and open PR";
    } finally {
      $("ghInput").value = "";
    }
  }

  // ------------------------------------------------------------------ past runs
  async function loadPastRuns() {
    const s = current();
    if (!s) return;
    const detail = await api("/api/sessions/" + s.id);
    const ul = clear($("pastList"));
    const runs = detail.runs || [];
    show($("pastRuns"), runs.length > 0);
    for (const r of runs) {
      ul.append(
        el(
          "li",
          {},
          el(
            "button",
            { type: "button", onclick: () => openPastRun(r) },
            el("span", { class: "t", text: r.task }),
            statusTag(r.status),
          ),
        ),
      );
    }
    const active = runs.find((r) => !TERMINAL.has(r.status));
    if (active && state.runId !== active.id) openPastRun(active);
  }
  async function openPastRun(r) {
    stopPolling();
    resetRunView();
    state.runId = r.id;
    state.run = r;
    show($("runPanel"));
    renderMeters();
    renderComposer();
    startPolling();
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    $("repoForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = $("repoUrl").value.trim();
      if (/^(sk-|AIza|ghp_|gho_|github_pat_)/.test(v)) {
        const b = $("repoError");
        b.textContent =
          "That looks like an API key or token. Paste a repository link here instead.";
        show(b);
        $("repoUrl").value = "";
        return;
      }
      $("repoBtn").disabled = true;
      const s = await addSession({ repo_url: v }, $("repoError"));
      if (s) $("repoUrl").value = "";
      $("repoBtn").disabled = false;
    });
    $("runBtn").addEventListener("click", startRun);
    $("cancelBtn").addEventListener("click", cancelRun);
    $("ghForm").addEventListener("submit", submitGh);
    $("ghCancel").addEventListener("click", () => {
      $("ghInput").value = "";
      closeDialog($("ghDialog"));
    });
    try {
      state.config = await api("/api/config");
    } catch (ex) {
      showBanner(ex.message);
      return;
    }
    if (!state.config.git_available)
      showBanner(
        "git is not installed on the RepoPilot server. Install git and restart to load repositories.",
      );
    const demos = clear($("demoList"));
    for (const d of state.config.demos) {
      demos.append(
        el(
          "button",
          {
            class: "demo-btn",
            type: "button",
            onclick: () => {
              $("task").value = d.task;
              addSession({ demo: d.id }, $("repoError"));
            },
          },
          el("strong", { text: d.title }),
          el("span", { text: d.description }),
        ),
      );
    }
    renderAi();
    try {
      await refreshSessions();
    } catch (ex) {
      showBanner(ex.message);
    }
    loadAi();
  }
  boot();
})();
