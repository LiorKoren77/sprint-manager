// Sprint Manager dashboard — vanilla JS, no build step.
//
// Three jobs: (1) poll /api/status and render the overview table, (2) open a per-ticket detail
// panel with a live WebSocket chat to that ticket's agent, (3) a round-robin "Next" button that
// jumps to the next agent whose activity is waiting_user.

const state = {
  rows: [],                 // latest /api/status rows
  openTickets: [],          // tickets with an open tab, in open order
  selected: null,           // currently shown ticket
  socket: null,             // WebSocket for the selected ticket
  fontPx: parseInt(localStorage.getItem("sm-font-px"), 10) || 13,  // transcript font size (persisted)
  queue: [],                // messages queued for the current ticket
  drafts: {},               // per-ticket unsent input drafts: ticket -> string
  projects: [],             // /api/projects rows: {name, repo, default, jira, ci}
  project: localStorage.getItem("sm-project") || "",  // table filter: "" = all projects
  taskTracker: "text",      // the New-task dialog's active tab
};

const $ = (id) => document.getElementById(id);

// ----- access token -----------------------------------------------------------------------
// The server refuses /api and /ws without the per-launch token it prints at startup (open the
// "…/?token=…" URL it shows). Take it from the URL once, remember it for reloads, and strip it from
// the address bar; every API call and WebSocket then carries it.
const TOKEN = (() => {
  const url = new URL(location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    try { localStorage.setItem("sm-token", fromUrl); } catch (_) {}
    url.searchParams.delete("token");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  try { return localStorage.getItem("sm-token") || ""; } catch (_) { return ""; }
})();

let tokenWarned = false;
const _fetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const headers = new Headers(init.headers || {});
  if (TOKEN) headers.set("X-SM-Token", TOKEN);
  const resp = await _fetch(input, { ...init, headers });
  if (resp.status === 401 && !tokenWarned) {
    tokenWarned = true;
    const banner = document.createElement("div");
    banner.className = "token-banner";
    banner.textContent = "Not authorized: open the dashboard with the URL the server printed at startup "
      + "(…/?token=…). The token changes every time the server restarts.";
    document.body.prepend(banner);
  }
  return resp;
};

// Values from the server end up in HTML; escape them all (agents can set some of them), and keep
// class-name fragments to a safe alphabet.
const cls = (v) => String(v ?? "").replace(/[^A-Za-z0-9_-]/g, "");

// ----- overview table ---------------------------------------------------------------------

let lastStatusText = null;  // raw /api/status body of the previous poll, to skip no-change renders

async function refreshStatus() {
  let rows, text;
  try {
    text = await (await fetch("/api/status")).text();
    rows = JSON.parse(text);
  } catch (_) {
    return; // service not up yet — leave the table as-is and try again next tick
  }
  state.rows = rows;
  // Once we know which tickets exist, reopen the tabs saved from a previous session (one-time).
  if (!tabsRestored && rows.length) restoreOpenTabs();
  // Rebuild the table/tabs DOM only when the server data actually changed — that rebuild is what
  // destroys any text selection the manager is making in a cell, so we skip it on no-change ticks.
  // The `working`/`queued` overlay lives in the body, so a stable turn correctly counts as unchanged.
  const changed = text !== lastStatusText;
  lastStatusText = text;
  if (changed) {
    renderTable();
    renderTabs();
  }
  // The queue is CLIENT state (sendChat adds to it) and can change with no server-body change, and
  // a ticket can sit stably at waiting_user — so this janitor + panel refresh must run EVERY tick,
  // never behind the no-change skip above (that skip once starved it, stranding queued messages).
  if (state.selected) {
    const selected = rowFor(state.selected);
    renderPanelHeader(selected);
    // Clear the queue once the selected ticket's turn has settled (no longer working/queued).
    if (selected && selected.activity !== "working" && selected.activity !== "queued") {
      if (state.queue.length > 0) clearQueue();
    }
  }
}

const rowFor = (ticket) => state.rows.find((r) => r.ticket === ticket);

function renderTable() {
  const table = $("overview-table");
  // Each ticket is its own <tbody class="issue"> (two rows: status line + full-width summary), so
  // a CSS :hover on the tbody highlights both rows and a click anywhere opens the ticket. Drop the
  // previously rendered groups (the <thead> stays).
  table.querySelectorAll("tbody.issue").forEach((b) => b.remove());
  for (const r of state.rows.filter((x) => !state.project || x.project === state.project)) {
    // Disable remove while a turn is in flight (working) or about to run (queued) — cutting the
    // agent off mid-turn is the one rough edge, so we just gate the button until it settles.
    const busy = r.activity === "working" || r.activity === "queued";
    const remove = busy
      ? `<button class="remove" disabled title="Busy — can't remove mid-turn">✕</button>`
      : `<button class="remove" title="Remove from dashboard"
          onclick="event.stopPropagation(); removeTicket('${cls(r.ticket)}')">✕</button>`;
    const group = document.createElement("tbody");
    group.className = "issue";
    group.onclick = () => openTicket(r.ticket);
    group.innerHTML = `
      <tr class="main">
        <td><b>${escapeHtml(r.ticket)}</b></td>
        <td>${escapeHtml(r.project || "")}</td>
        <td>${escapeHtml(r.kind || ((r.issue_type || "").toLowerCase() === "bug" ? "bug" : "feature"))}</td>
        <td>${escapeHtml(trackerLabel(r))}</td>
        <td><span class="stage">${escapeHtml(r.stage)}</span></td>
        <td><span class="badge activity-${cls(r.activity)}">${escapeHtml(r.activity)}</span></td>
        <td class="note">${escapeHtml(r.note || "")}</td>
        <td>${escapeHtml(r.ci_status || "")}</td>
        <td>$${(r.cost_usd || 0).toFixed(2)}</td>
        <td>${remove}</td>
      </tr>
      <tr class="summary"><td colspan="11">↳ ${escapeHtml(r.summary || "")}</td></tr>`;
    table.appendChild(group);
  }
}

// "jira: In Review" / "github: open" / "text" — which tracker a task lives in, and its status there.
function trackerLabel(r) {
  const t = r.tracker || "jira";
  const st = r.tracker_status || r.jira_status || "";
  return st ? `${t}: ${st}` : t;
}

// ----- tabs + round-robin -----------------------------------------------------------------

function renderTabs() {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  for (const ticket of state.openTickets) {
    const r = rowFor(ticket) || { activity: "idle" };
    const tab = document.createElement("div");
    tab.className = "tab" + (ticket === state.selected ? " active" : "");
    tab.onclick = () => openTicket(ticket);
    // Plain label so the whole tab is clickable for toggling (the Jira link lives on the
    // issue title inside the panel, not here). For pr-open tickets, two extra dots show CI and
    // review status at a glance — visible from any page since tabs always render.
    tab.innerHTML = `<span class="dot activity-${cls(r.activity)}"></span>${escapeHtml(ticket)}${prLights(r)}<span class="x" title="Close tab">×</span>`;
    tab.querySelector(".x").onclick = (e) => { e.stopPropagation(); closeTab(ticket); };
    // The review dot is clickable (pr-open only): re-arm the review channel without opening the tab.
    const rv = tab.querySelector(".pr-light-review");
    if (rv) rv.onclick = (e) => { e.stopPropagation(); rearmReview(ticket); };
    tabs.appendChild(tab);
  }
}

// The two pr-open status dots (CI + review) for a tab. Empty for non-pr-open tickets.
// BOTH dots reflect the ARMED state, not just the last-known verdict: coloured only while that
// channel has FIRED and not yet been re-armed, grey ("watching") otherwise. Without this gating, a
// re-arm (e.g. the agent pushes a fix and declares waiting_external again) resets the *_fired flag
// on the backend but the dot would keep showing the stale prior verdict forever — indistinguishable
// from "nothing happened" once the next build lands on the same verdict (e.g. passed → passed).
function prLights(r) {
  if (!r || r.stage !== "pr-open") return "";
  const ci = ciLightState(r);
  const review = reviewLightState(r);
  return `<span class="pr-light ci-${cls(ci)}" title="${escapeHtml(ciTitle(r))}"></span>`
       + `<span class="pr-light pr-light-review review-${cls(review)}" title="${escapeHtml(reviewTitle(r))}"></span>`;
}

// The CI indicator's colour key: grey ("none") while armed/watching for the next build result; the
// verdict's colour once a fresh terminal result (passed/failed) has fired.
function ciLightState(r) {
  return r.ci_fired ? (r.ci_status || "none") : "none";
}

function ciTitle(r) {
  return r.ci_fired
    ? `CI: ${r.ci_status || "unknown"}`
    : "CI: watching for the next build result";
}

// The review indicator's colour key: grey ("none") while armed/watching; the decision colour once a
// review signal has fired (a bare comment fire with no formal decision shows as "commented"/blue).
function reviewLightState(r) {
  if (!r.review_fired) return "none";
  const d = r.review_decision;
  return d && d !== "none" ? d : "commented";
}

function reviewTitle(r) {
  return r.review_fired
    ? `Review: ${r.review_decision || "comment"} — click to re-arm (keep watching for the next one)`
    : "Review: watching for the next comment/approval/rejection";
}

// Close a ticket's tab; if it was the open one, fall back to another tab (or hide the panel).
function closeTab(ticket) {
  state.openTickets = state.openTickets.filter((t) => t !== ticket);
  if (state.selected === ticket) {
    // Save the closing ticket's unsent draft and clear the box BEFORE reassigning selected —
    // otherwise openTicket(fallback) would store this ticket's text under the fallback ticket.
    state.drafts[ticket] = $("chat-input").value;
    $("chat-input").value = "";
    state.selected = null;
    if (state.socket) { state.socket.close(); state.socket = null; }
    const next = state.openTickets[state.openTickets.length - 1] || null;
    if (next) openTicket(next);
    else {
      $("panel").classList.add("hidden");
      setMaximized(false);  // no panel to show → drop out of full-screen so nothing's stranded
    }
  }
  saveOpenTabs();
  renderTabs();
}

// Persist the open tabs + current selection to localStorage (per-browser view state) so a page
// reload — e.g. after a server restart — reopens them instead of starting blank.
function saveOpenTabs() {
  try {
    localStorage.setItem("sm-open-tabs",
      JSON.stringify({ open: state.openTickets, selected: state.selected }));
  } catch (_) { /* storage full/disabled — non-fatal */ }
}

// Reopen the saved tabs once the first status poll tells us which tickets still exist (a ticket may
// have been removed since). Runs exactly once, on the first poll that returns rows.
let tabsRestored = false;
function restoreOpenTabs() {
  tabsRestored = true;
  let saved;
  try { saved = JSON.parse(localStorage.getItem("sm-open-tabs") || "null"); } catch (_) { return; }
  if (!saved || !Array.isArray(saved.open)) return;
  const exists = new Set(state.rows.map((r) => r.ticket));
  const open = saved.open.filter((t) => exists.has(t));  // drop tabs for tickets that are gone
  if (!open.length) return;
  state.openTickets = open;
  renderTabs();
  openTicket(exists.has(saved.selected) ? saved.selected : open[open.length - 1]);
}

// Cycle to the next ticket that needs the human: waiting_user, or parked at idle in a working
// stage (a goto-stage jump awaiting Start) — without the latter a jumped ticket silently stalls.
function needsYou(r) {
  return r.activity === "waiting_user"
    || (r.activity === "idle" && r.stage !== "to-do" && r.stage !== "done");
}

function nextNeedsYou() {
  const waiting = state.rows.filter(needsYou).map((r) => r.ticket);
  if (waiting.length === 0) return;
  const start = waiting.indexOf(state.selected);
  openTicket(waiting[(start + 1) % waiting.length]);
}

// ----- per-ticket detail panel + chat -----------------------------------------------------

async function openTicket(ticket) {
  if (state.selected) state.drafts[state.selected] = $("chat-input").value;
  state.selected = ticket;
  if (!state.openTickets.includes(ticket)) state.openTickets.push(ticket);
  saveOpenTabs();  // persist open tabs + selection so a reload/restart restores them
  renderTabs();

  $("panel").classList.remove("hidden");
  $("transcript").innerHTML = "";
  clearQueue();  // clear queue when switching tickets
  renderPanelHeader(rowFor(ticket));

  // Replay stored transcript, then attach the live socket.
  const detail = await (await fetch(`/api/ticket/${ticket}`)).json();
  (detail.transcript || []).forEach(addEvent);
  connectSocket(ticket);

  const input = $("chat-input");
  input.value = state.drafts[ticket] || "";
  autoGrowChat();
}

function renderPanelHeader(r) {
  if (!r) return;
  const jira = $("panel-jira");
  jira.textContent = `${r.ticket}: ${r.summary || ""}`;
  // The server supplies url from the task's tracker (Jira browse link, GitHub issue, Slack thread);
  // a text task has none, so the title is then plain text rather than a dead link.
  if (r.url) jira.href = r.url; else jira.removeAttribute("href");
  $("panel-edit-text").classList.toggle("hidden", r.tracker !== "text");
  $("panel-file-issue").classList.toggle("hidden", r.tracker !== "text" && r.tracker !== "slack");
  setBadge($("panel-stage"), "stage", r.stage);

  // PR link — shown only once a PR is open. Label "PR #<n>" (number parsed from the GitHub URL);
  // opens the PR in a new tab. Hidden (no URL) until Ship ▶ opens it.
  const prEl = $("panel-pr");
  const prNum = (r.pr_url || "").match(/\/pull\/(\d+)/);
  if (r.pr_url && prNum && /^https:\/\//.test(r.pr_url)) {  // only real web links, never javascript:
    prEl.href = r.pr_url;
    prEl.textContent = `PR #${prNum[1]}`;
    prEl.classList.remove("hidden");
  } else {
    prEl.classList.add("hidden");
  }

  setBadge($("panel-activity"), `activity-${cls(r.activity)}`, r.activity);

  // CI + review status badges — shown only in pr-open, driven by the same fields as the tab dots.
  // Both badges mirror their tab dot: coloured while that channel has fired, grey ("waiting") once
  // armed/re-armed. The review badge is clickable to re-arm (dismiss a useless review event).
  const ciEl = $("panel-ci"), rvEl = $("panel-review");
  if (r.stage === "pr-open") {
    const ci = ciLightState(r), review = reviewLightState(r);
    ciEl.className = `pr-badge ci-${ci}`;
    ciEl.textContent = r.ci_fired ? `CI: ${r.ci_status || "unknown"}` : "CI: waiting";
    ciEl.title = ciTitle(r);
    rvEl.className = `pr-badge pr-badge-review review-${review}`;
    rvEl.textContent = r.review_fired ? `review: ${r.review_decision || "comment"}` : "review: waiting";
    rvEl.title = reviewTitle(r);
  } else {
    ciEl.className = "pr-badge hidden";
    rvEl.className = "pr-badge hidden";
    ciEl.title = "";
    rvEl.title = "";
  }

  // Context indicator: the tokens the session's latest API call processed — what every next call
  // re-reads — amber past the server's threshold. (The cumulative cache_read only ever grows, so it
  // can't say whether the session is big NOW; it stays in the tooltip for comparison.)
  const cacheEl = $("panel-cache");
  const ctx = r.context_tokens || 0;
  if (ctx > 0) {
    const warnAt = (appConfig && appConfig.context_warn_tokens) || 150000;
    cacheEl.textContent = `context: ${Math.round(ctx / 1000)}k`;
    cacheEl.title = `Latest call: ${ctx.toLocaleString()} tokens · session peak: `
      + `${(r.peak_context_tokens || 0).toLocaleString()} · cumulative cache read: `
      + `${((r.cache_read_tokens || 0) / 1_000_000).toFixed(1)}M. Amber above `
      + `${Math.round(warnAt / 1000)}k — consider ⟳ Compact.`;
    cacheEl.className = "cache-indicator" + (ctx > warnAt ? " warning" : "");
  } else {
    cacheEl.className = "cache-indicator empty";
    cacheEl.title = "";
  }

  // Start shows for a not-yet-started ticket (stage to-do) AND for a stage-jumped one (goto-stage
  // resets activity to idle with no session — Start begins the fresh session there). Approve shows
  // once the agent is waiting on your decision. At most one is visible at a time.
  const notStarted = r.stage === "to-do";
  const needsStart = notStarted || (r.activity === "idle" && r.stage !== "done");
  const resting = r.activity === "waiting_user" || r.activity === "waiting_external";
  $("panel-start").classList.toggle("hidden", !needsStart);
  // work never advances by Approve: inside it, chat moves between the agent's gates, and leaving it
  // is Ship ▶ (push + open the PR). The Approve label says what it approves in each stage.
  $("panel-approve").classList.toggle("hidden", needsStart || !resting || r.stage === "work");
  $("panel-approve").textContent = APPROVE_LABELS[r.stage] || "Approve → next stage ▶";
  $("panel-ship").classList.toggle("hidden", needsStart || !resting || r.stage !== "work");
  // The CI button: pr-open only, whenever the agent isn't mid-turn. Its meaning follows the project's
  // CI provider — Jenkins: "Trigger CI" (the only way a build starts); GitHub checks run on every
  // push, so it re-runs failed runs; a project with no CI gets no button.
  const ciProvider = ((state.projects.find((p) => p.name === r.project) || {}).ci) || "github";
  $("panel-trigger-ci").textContent = ciProvider === "jenkins" ? "Trigger CI" : "Re-run CI";
  $("panel-trigger-ci").classList.toggle("hidden",
    needsStart || !resting || r.stage !== "pr-open" || ciProvider === "none");
  // Stop interrupts the in-flight turn; shown only while the agent is actively working.
  $("panel-stop").classList.toggle("hidden", r.activity !== "working");
  // Compact is available whenever a stage is active (not to-do/done/working) to reset context.
  // Hidden while idle too: a stage-jumped ticket has no session yet — Start is the right button.
  const inActiveStage = !notStarted && r.stage !== "done";
  $("panel-compact").classList.toggle("hidden", !inActiveStage || r.activity === "working" || r.activity === "idle");
  // Go-to-stage select: visible whenever a ticket is active; reset to placeholder on panel refresh.
  $("panel-goto-stage").classList.toggle("hidden", !inActiveStage || r.activity === "working");
  $("panel-goto-stage").value = "";
}

const APPROVE_LABELS = {
  "explore": "Approve plan → work ▶",
  "pr-open": "Approve (merged) → done ✓",
};

function setBadge(el, cls, text) { el.className = "badge " + cls; el.textContent = text; }

function connectSocket(ticket) {
  if (state.socket) state.socket.close();
  const socket = new WebSocket(`ws://${location.host}/ws/${encodeURIComponent(ticket)}?token=${encodeURIComponent(TOKEN)}`);
  socket.onmessage = (e) => addEvent(JSON.parse(e.data));
  // A closed socket (server restart, network blip) used to just sit dead — sendChat's only guard
  // was a truthy check on the object reference, which stays true even once it's closed, so a typed
  // message would silently vanish (queued client-side, never actually sent) with no feedback at
  // all. Auto-reconnect and say so, but only if this socket is still the active one for the
  // currently open ticket — closing it on purpose (switching tabs) already replaced state.socket.
  socket.onclose = () => {
    if (state.socket === socket && state.selected === ticket) {
      addEvent({ kind: "system", text: "⚠️ Connection lost — reconnecting…" });
      setTimeout(() => { if (state.selected === ticket) connectSocket(ticket); }, 1500);
    }
  };
  state.socket = socket;
}

function addEvent(ev) {
  if (ev.kind === "status") {
    // Stage/activity update pushed by the backend at the end of every turn — update the UI
    // immediately instead of waiting for the next /api/status poll. Render the MERGED row, not
    // the raw payload: the push carries only TicketStatus fields, while the poll rows are
    // enriched with url/jira_status — rendering raw would drop those until the next poll.
    try {
      const r = JSON.parse(ev.text);
      const idx = state.rows.findIndex((x) => x.ticket === r.ticket);
      if (idx >= 0) state.rows[idx] = { ...state.rows[idx], ...r };
      const merged = idx >= 0 ? state.rows[idx] : r;
      if (state.selected === merged.ticket) renderPanelHeader(merged);
      renderTable();
      renderTabs();
    } catch (_) {}
    return;
  }
  const div = document.createElement("div");
  div.className = `ev ev-${ev.kind}`;
  const label = { user: "you", text: "", thinking: "thinking", tool: "tool", result: "done", system: "system" }[ev.kind];
  div.textContent = (label ? `[${label}] ` : "") + ev.text;
  const t = $("transcript");
  t.appendChild(div);
  t.scrollTop = t.scrollHeight;
}

// Resize the chat box to fit its content (and reflow on width changes). CSS max-height + overflow
// cap the growth and add a scrollbar past that.
function autoGrowChat() {
  const el = $("chat-input");
  el.style.height = "auto";              // shrink first so it can also get smaller
  el.style.height = el.scrollHeight + "px";
}

function renderQueue() {
  const display = $("queue-display");
  if (state.queue.length === 0) {
    display.classList.add("hidden");
    return;
  }
  display.classList.remove("hidden");
  display.innerHTML = '<span class="queue-label">⏳ Queued messages (' + state.queue.length + '):</span>' +
    state.queue.map((msg) => '<div class="queue-item">' + escapeHtml(msg) + '</div>').join("");
}

function addToQueue(message) {
  state.queue.push(message);
  renderQueue();
}

function clearQueue() {
  state.queue = [];
  renderQueue();
}

function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  // readyState, not just a truthy check: a CLOSED socket is still a non-null object, and send() on
  // one is a silent no-op (no exception) — the old `!state.socket` guard let that through, clearing
  // the input and "queuing" a message that had already vanished. Never clear the box on failure —
  // the manager's typed text must survive a dead connection, not disappear with it.
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    addEvent({ kind: "system", text: "Not connected — reconnecting. Your message is still in the box; try again in a moment." });
    if (state.selected) connectSocket(state.selected);
    return;
  }
  state.socket.send(text);
  addToQueue(text);  // show it in the queue display
  input.value = "";
  autoGrowChat();                        // collapse back to one line after sending
}

// POST helper for the panel actions: parses JSON, and surfaces any failure (network error or a
// 4xx/5xx like goto-stage's "Unknown stage") as a system line in the transcript instead of
// silently doing nothing. Returns the parsed body, or null on failure.
async function postJSON(url, body) {
  let resp, res = {};
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    res = await resp.json().catch(() => ({}));
  } catch (_) {
    addEvent({ kind: "system", text: `Request failed (network): ${url}` });
    return null;
  }
  if (!resp.ok) {
    addEvent({ kind: "system", text: res.error || res.detail || `Request failed (${resp.status}): ${url}` });
    return null;
  }
  return res;
}

// Start = the gate from "to-do" → explore (the first stage), shown as a panel button like Approve.
async function startTicket(ticket) {
  if (!ticket) return;
  $("panel-start").classList.add("hidden");  // optimistic: it's about to advance to explore
  await postJSON(`/api/start/${ticket}`);
  refreshStatus();
}

// Remove a ticket from the dashboard entirely (state, transcript, notes). Destructive → confirm.
// The git worktree/branch is left untouched; if one existed, we surface its path afterward.
async function removeTicket(ticket) {
  if (!confirm(`Remove ${ticket} from the dashboard?\n\nThis deletes its saved state, transcript and `
    + `notes here. A git worktree/branch (if any) is left untouched.`)) return;
  let resp, res = {};
  try {
    resp = await fetch(`/api/ticket/${ticket}`, { method: "DELETE" });
    res = await resp.json().catch(() => ({}));
  } catch (_) { $("add-msg").textContent = "remove failed"; return; }
  if (!resp.ok || !res.removed) {
    // The delete didn't actually happen (e.g. server not restarted) — say so, leave the row.
    $("add-msg").textContent =
      res.error || res.detail || "remove failed — is the server up to date? try restarting it";
    return;
  }
  // Drop it from the main list immediately and re-render, then reconcile with the server.
  state.rows = state.rows.filter((r) => r.ticket !== ticket);
  renderTable();
  closeTab(ticket);  // also closes the socket and falls back if it was the open tab
  $("add-msg").textContent = res.worktree
    ? `Removed ${ticket}. Worktree left at ${res.worktree}`
    : `Removed ${ticket}.`;
  refreshStatus();
}

// Interrupt the in-flight turn. The session is preserved — just type a corrected message to resend.
async function interruptTicket(ticket) {
  if (!ticket) return;
  $("panel-stop").classList.add("hidden");  // optimistic; the next poll reflects the settled state
  await postJSON(`/api/interrupt/${ticket}`);
}

// Approve: explore → work (plan approved) or pr-open → done (PR merged). Not offered in work.
async function approveAndContinue() {
  if (!state.selected) return;
  // Hide the button the instant it's clicked: instant feedback + no double-click while the backend
  // does the (sometimes slow) dispose → Jira → worktree → agent-connect before the turn shows as
  // working. The backend also flips the ticket to "working" immediately and ignores duplicates.
  $("panel-approve").classList.add("hidden");
  await postJSON(`/api/approve/${state.selected}`);
}

// Ship ▶ (work only): the backend checks the worktree is committed, asks the session for a final
// recap + PR title/body, pushes, opens the PR, and moves the ticket to pr-open. CI is NOT started.
async function shipTicket() {
  if (!state.selected) return;
  if (!confirm(`Ship ${state.selected}?\n\nThis pushes the branch, opens (or updates) the PR, and `
    + `updates the task's tracker. CI is not started — use Trigger CI afterwards.`)) return;
  $("panel-ship").classList.add("hidden");  // optimistic; backend flips to working at once
  await postJSON(`/api/ship/${state.selected}`);
}

// Trigger CI (pr-open only): start a Jenkins build for the open PR and re-arm the poll.
async function triggerCI() {
  if (!state.selected) return;
  $("panel-trigger-ci").classList.add("hidden");
  const res = await postJSON(`/api/trigger-ci/${state.selected}`);
  if (!res || !res.triggered) $("panel-trigger-ci").classList.remove("hidden");  // reason is in the transcript
  refreshStatus();
}

// Compact the current stage: save the last summary to notes and restart the stage fresh.
async function compactStage() {
  if (state.selected) await postJSON(`/api/compact/${state.selected}`);
}

// Re-arm the code-review poll channel (pr-open): dismiss a useless review event and keep watching
// for the next comment/approval/rejection. The backend keeps the watermarks so the event we just
// saw won't re-fire; the review indicator goes grey and polling resumes. Backend pushes a status
// event, so the UI updates immediately (refreshStatus is a belt-and-braces reconcile).
async function rearmReview(ticket) {
  if (!ticket) return;
  await postJSON(`/api/rearm-review/${ticket}`);
  refreshStatus();
}

// Jump to any stage: dispose the current session and reset so Start begins a fresh session there.
async function gotoStage(stage) {
  if (!state.selected || !stage) return;
  // Hide the select immediately: instant feedback + no duplicate jump while the backend disposes
  // the session and resets. The backend also flips to working at once and ignores duplicates; it
  // reappears once the ticket lands on idle at the new stage.
  $("panel-goto-stage").classList.add("hidden");
  await postJSON(`/api/goto-stage/${state.selected}`, { stage });
}

// ----- sprint loading + wiring ------------------------------------------------------------

// Populate the sprint picker with the board's current + upcoming sprints, and pre-fill the
// current one so a single click of "Load sprint" works.
async function loadSprintOptions() {
  const proj = state.projects.find((p) => p.name === selectedProjectName());
  $("jira-loader").classList.toggle("hidden", !(proj && proj.jira));
  if (!proj || !proj.jira) return;
  try {
    const sprints = await (await fetch(`/api/sprints?project=${encodeURIComponent(proj.name)}`)).json();
    if (!Array.isArray(sprints)) return;
    const sel = $("sprint");
    const prev = sel.value;                    // keep the user's pick across refreshes
    sel.innerHTML = "";
    for (const s of sprints) {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.label ? `${s.name} (${s.label})` : s.name;  // e.g. "… (current)" / "… (next)"
      sel.appendChild(opt);
    }
    // Default to the current sprint, but don't clobber an explicit earlier choice.
    const current = sprints.find((s) => s.label === "current") || sprints[0];
    sel.value = (prev && sprints.some((s) => s.name === prev)) ? prev : (current ? current.name : "");
  } catch (_) { /* Jira not configured yet — leave the dropdown empty */ }
}

async function loadSprint() {
  const sprint = $("sprint").value.trim();
  if (!sprint) return;
  $("load-msg").textContent = "loading…";
  try {
    const res = await (await fetch("/api/load", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sprint, project: selectedProjectName() }),
    })).json();
    $("load-msg").textContent = `loaded ${res.loaded} tickets`;
    refreshStatus();
  } catch (e) {
    $("load-msg").textContent = "load failed (check Jira creds / VPN)";
  }
}

// ----- projects + new tasks ---------------------------------------------------------------

// The project new tasks / sprints go to: the filter's project, else the server's default.
function selectedProjectName() {
  if (state.project) return state.project;
  const d = state.projects.find((p) => p.default) || state.projects[0];
  return d ? d.name : "";
}

async function loadProjects() {
  let data;
  try { data = await (await fetch("/api/projects")).json(); } catch (_) { return; }
  state.projects = (data.projects || []).filter((p) => !p.error);
  if (state.project && !state.projects.some((p) => p.name === state.project)) state.project = "";
  const opts = state.projects.map((p) => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join("");
  $("project").innerHTML = `<option value="">All projects</option>` + opts;
  $("project").value = state.project;
  $("task-project").innerHTML = opts;
  if (!state.projects.length) $("add-msg").textContent = "No projects yet — ＋ Project to register a repo.";
  loadSprintOptions();
}

function onProjectFilter() {
  state.project = $("project").value;
  try { localStorage.setItem("sm-project", state.project); } catch (_) {}
  renderTable();
  loadSprintOptions();
}

async function addProject() {
  const repo = prompt("Path of the git checkout to register as a project:");
  if (!repo) return;
  const res = await postJSON("/api/projects", { repo: repo.trim() });
  if (res) { $("add-msg").textContent = `registered project ${res.name}`; await loadProjects(); }
  else $("add-msg").textContent = "couldn't register — see the transcript/console";
}

function setTaskTab(tracker) {
  state.taskTracker = tracker;
  document.querySelectorAll(".task-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tracker === tracker));
  document.querySelectorAll("#task-form section").forEach((sec) =>
    sec.classList.toggle("hidden", sec.dataset.for !== tracker));
  const proj = state.projects.find((p) => p.name === $("task-project").value);
  $("task-jira-hint").textContent = proj && !proj.jira
    ? `Project ${proj.name} has no [jira] table in its profile, so Jira issues can't be added to it.` : "";
  $("task-create").textContent = tracker === "jira" ? "Add issue" : "Create";
}

function openTaskDialog() {
  $("task-msg").textContent = "";
  $("task-project").value = selectedProjectName();
  setTaskTab(state.taskTracker);
  $("task-dialog").showModal();
}

// POST that reports into a given message element instead of the transcript. Returns body or null.
async function postTo(url, body, msgEl, method = "POST") {
  msgEl.textContent = "working…";
  try {
    const resp = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify(body) });
    const res = await resp.json().catch(() => ({}));
    if (!resp.ok) { msgEl.textContent = res.error || res.detail || `failed (${resp.status})`; return null; }
    msgEl.textContent = "";
    return res;
  } catch (_) { msgEl.textContent = "request failed (network)"; return null; }
}

async function createTask() {
  const project = $("task-project").value;
  const t = state.taskTracker;
  let res;
  if (t === "jira") {
    res = await postTo("/api/add", { url: $("issue-url").value.trim(), project }, $("task-msg"));
  } else if (t === "text") {
    res = await postTo("/api/tasks", { project, tracker: "text", title: $("task-title").value,
      kind: $("task-kind").value, body: $("task-body").value }, $("task-msg"));
  } else if (t === "github") {
    res = await postTo("/api/tasks", { project, tracker: "github", ref: $("task-gh-ref").value.trim() },
                       $("task-msg"));
  } else {
    res = await postTo("/api/tasks", { project, tracker: "slack", ref: $("task-slack-ref").value.trim(),
      title: $("task-slack-title").value, kind: $("task-slack-kind").value }, $("task-msg"));
  }
  if (!res) return;
  $("task-dialog").close();
  ["task-title", "task-body", "task-gh-ref", "task-slack-ref", "task-slack-title", "issue-url"]
    .forEach((id) => { $(id).value = ""; });
  $("add-msg").textContent = res.warning || (res.existing ? `${res.key} already exists` : `added ${res.key}`);
  await refreshStatus();
  if (res.key) openTicket(res.key);
}

async function loadGithubIssues() {
  const res = await postTo("/api/load-issues", { project: $("task-project").value,
    assignee: $("task-gh-assignee").value.trim(), label: $("task-gh-label").value.trim() }, $("task-msg"));
  if (!res) return;
  $("task-dialog").close();
  $("add-msg").textContent = `loaded ${res.loaded} GitHub issue(s)`;
  refreshStatus();
}

async function openTextEditor() {
  if (!state.selected) return;
  $("text-dialog-ticket").textContent = state.selected;
  $("text-msg").textContent = "";
  const res = await (await fetch(`/api/task-text/${state.selected}`)).json().catch(() => ({}));
  $("text-body").value = res.body || "";
  $("text-dialog").showModal();
}

async function fileAsIssue() {
  if (!state.selected) return;
  if (!confirm(`Create a GitHub issue from ${state.selected} (its problem statement + acceptance `
    + `criteria)? The task then tracks that issue, and its PR will close it on merge.`)) return;
  const res = await postJSON(`/api/file-issue/${state.selected}`);
  if (res) refreshStatus();
}

async function saveTaskText() {
  const res = await postTo(`/api/task-text/${state.selected}`, { body: $("text-body").value },
                           $("text-msg"), "PUT");
  if (res) $("text-dialog").close();
}

// ----- theme (dark / light, persisted) ----------------------------------------------------

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $("theme-toggle").textContent = theme === "dark" ? "☀️" : "🌙";  // shows what you'd switch TO
}

function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem("sm-theme", next);
  applyTheme(next);
}

// ----- transcript font size (persisted) ---------------------------------------------------

function applyFontSize() {
  $("transcript").style.fontSize = state.fontPx + "px";
}

function bumpFont(delta) {
  state.fontPx = Math.max(9, Math.min(28, state.fontPx + delta));  // clamp to a sane range
  localStorage.setItem("sm-font-px", state.fontPx);
  applyFontSize();
}

// ----- workspace sizing: drag-resizable transcript + full-screen maximize -----------------

// Restore the transcript height the user last dragged to (native CSS resize sets an inline height).
function applyTranscriptHeight() {
  const h = localStorage.getItem("sm-transcript-h");
  if (h) $("transcript").style.height = h;
}

// Maximize = the open ticket's panel fills the whole viewport (header + overview hidden). The CSS
// (body.workspace-max) does the layout; here we just flip the class and reflect it on the button.
function setMaximized(on) {
  document.body.classList.toggle("workspace-max", on);
  const btn = $("panel-max");
  btn.textContent = on ? "⤢" : "⛶";
  btn.title = on ? "Restore workspace (Esc)" : "Expand workspace to full screen (Esc to exit)";
}
function toggleMaximize() { setMaximized(!document.body.classList.contains("workspace-max")); }

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ----- Settings drawer ---------------------------------------------------------------

// Stage names, model ids/labels, and the no-effort model set all come from GET /api/config
// (sourced from models.py) — nothing is hardcoded here, so a backend rename/model bump can't
// leave the UI listing stale values.
const EFFORT_OPTIONS = ["low", "medium", "high"];
let appConfig = null;  // last /api/config payload

async function fetchAppConfig() {
  appConfig = await (await fetch("/api/config")).json();
  return appConfig;
}

// Populate the go-to-stage select from the server's working-stage list (placeholder kept).
function renderGotoStageOptions(stages) {
  const sel = $("panel-goto-stage");
  sel.innerHTML = `<option value="">↩ go to stage…</option>` +
    stages.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
}

function openSettings() {
  $("settings-overlay").classList.remove("hidden");
  $("settings-drawer").classList.add("open");
  loadSettingsConfig();
}

function closeSettings() {
  $("settings-overlay").classList.add("hidden");
  $("settings-drawer").classList.remove("open");
}

function switchSettingsSection(id) {
  document.querySelectorAll(".settings-section").forEach((s) =>
    s.classList.toggle("active", s.id === "settings-" + id));
  document.querySelectorAll(".settings-nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.section === id));
}

async function loadSettingsConfig() {
  const cfg = await fetchAppConfig();
  renderGotoStageOptions(cfg.stages || []);  // keep the jump select in sync too
  renderModelsSection(cfg);
  loadCredentialsSection();  // separate endpoint — never bundled into /api/config's cached shape
}

// ----- Credentials section: .env-backed fields + read-only external login status ---------------

async function loadCredentialsSection() {
  const data = await (await fetch("/api/config/credentials")).json();
  renderCredentialsSection(data);
}

function renderCredentialsSection(data) {
  const sec = $("settings-credentials");
  const groups = {};
  for (const f of data.fields || []) {
    (groups[f.group] ||= []).push(f);
  }
  const fieldRow = (f) => `
    <div class="cred-row">
      <label>${escapeHtml(f.label)}${f.set ? ' <span class="cred-set">✓ ' + escapeHtml(f.value) + '</span>' : ''}</label>
      <input type="${f.secret ? "password" : "text"}" class="cred-input" data-key="${escapeHtml(f.key)}"
             placeholder="${f.set ? "(leave blank to keep current)" : "(not set)"}" />
    </div>`;
  const groupsHtml = Object.entries(groups).map(([group, fields]) => `
    <div class="cred-group">
      <h4 class="cred-group-title">${group}</h4>
      ${fields.map(fieldRow).join("")}
    </div>`).join("");

  const externalHtml = (data.external || []).map((e) => `
    <div class="cred-external-row">
      <span class="cred-dot ${e.ok ? "ok" : "missing"}"></span>
      <span>${e.label}</span>
      ${e.ok ? "" : `<span class="cred-hint">${escapeHtml(e.hint)}</span>`}
    </div>`).join("");

  sec.innerHTML = `
    <h3 class="settings-section-title">Credentials</h3>
    <p class="settings-hint">Stored in <code>.env</code> at the app root (gitignored). Leave a
      field blank to leave it unchanged. A real shell-exported environment variable always takes
      precedence over <code>.env</code>.</p>
    ${groupsHtml}
    <div class="settings-actions">
      <span id="sm-cred-status" class="settings-status"></span>
      <button id="sm-cred-save" class="settings-save-btn">Save</button>
    </div>
    <h3 class="settings-section-title cred-external-title">External logins</h3>
    ${externalHtml}`;

  $("sm-cred-save").onclick = saveCredentials;
}

async function saveCredentials() {
  const sec = $("settings-credentials");
  const inputs = [...sec.querySelectorAll(".cred-input")].filter((i) => i.value !== "");
  const status = $("sm-cred-status");
  if (!inputs.length) {
    status.textContent = "Nothing changed";
    status.className = "settings-status";
    setTimeout(() => { status.textContent = ""; }, 2000);
    return;
  }
  try {
    for (const input of inputs) {
      const res = await fetch("/api/config/credentials", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: input.dataset.key, value: input.value }),
      });
      if (!res.ok) throw new Error("save failed");
    }
    status.textContent = "✓ Saved — applied immediately, no restart needed";
    status.className = "settings-status ok";
    loadCredentialsSection();  // re-render with fresh masked/"set" state
  } catch (_) {
    status.textContent = "Save failed";
    status.className = "settings-status err";
  }
  setTimeout(() => { status.textContent = ""; }, 4000);
}

function renderModelsSection(cfg) {
  const sec = $("settings-models");
  const noEffort = new Set(cfg.no_effort_models || []);
  // Single-pass render: `selected`/`disabled` are emitted directly in the template, so there is
  // no separate restore pass that could drift from it.
  const modelOpts = (sel) => (cfg.model_options || []).map((o) =>
    `<option value="${escapeHtml(o.value)}"${o.value === sel ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("");
  const effortOpts = (sel) => `<option value="">—</option>` +
    EFFORT_OPTIONS.map((e) => `<option value="${e}"${e === sel ? " selected" : ""}>${e}</option>`).join("");

  sec.innerHTML = `
    <h3 class="settings-section-title">Models per stage</h3>
    <table class="settings-table">
      <thead><tr><th>Stage</th><th>Model</th><th>Effort</th></tr></thead>
      <tbody>
        ${cfg.models.map((row) => `<tr>
            <td class="settings-stage-cell">${escapeHtml(row.stage)}</td>
            <td><select class="sm-model" data-stage="${escapeHtml(row.stage)}">${modelOpts(row.model)}</select></td>
            <td><select class="sm-effort" data-stage="${escapeHtml(row.stage)}"${noEffort.has(row.model) ? " disabled" : ""}>${effortOpts(row.effort || "")}</select></td>
          </tr>`).join("")}
      </tbody>
    </table>
    <div class="settings-actions">
      <span id="sm-models-status" class="settings-status"></span>
      <button id="sm-models-save" class="settings-save-btn">Save</button>
    </div>`;

  // Disable effort when a no-effort model (e.g. Haiku) is selected.
  sec.querySelectorAll(".sm-model").forEach((mSel) => {
    mSel.addEventListener("change", () => {
      const eSel = sec.querySelector(`.sm-effort[data-stage="${mSel.dataset.stage}"]`);
      const off = noEffort.has(mSel.value);
      eSel.disabled = off;
      if (off) eSel.value = "";
    });
  });

  $("sm-models-save").onclick = saveModelsConfig;
}

async function saveModelsConfig() {
  const sec = $("settings-models");
  const items = [...sec.querySelectorAll(".sm-model")].map((mSel) => {
    const eSel = sec.querySelector(`.sm-effort[data-stage="${mSel.dataset.stage}"]`);
    return { stage: mSel.dataset.stage, model: mSel.value, effort: eSel.value || null };
  });
  const status = $("sm-models-status");
  try {
    const res = await fetch("/api/config/models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(items),
    });
    const data = await res.json();
    status.textContent = data.saved ? "✓ Saved — takes effect on next stage start" : "Error";
    status.className = "settings-status " + (data.saved ? "ok" : "err");
  } catch (_) {
    status.textContent = "Save failed";
    status.className = "settings-status err";
  }
  setTimeout(() => { status.textContent = ""; }, 4000);
}

$("load").onclick = loadSprint;
$("issue-url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); createTask(); } });
$("next").onclick = nextNeedsYou;
$("chat-send").onclick = sendChat;
$("panel-start").onclick = () => startTicket(state.selected);
$("panel-approve").onclick = approveAndContinue;
$("panel-stop").onclick = () => interruptTicket(state.selected);
$("panel-compact").onclick = compactStage;
$("panel-ship").onclick = shipTicket;
$("panel-trigger-ci").onclick = triggerCI;
$("panel-review").onclick = () => { if (state.selected) rearmReview(state.selected); };
$("panel-goto-stage").onchange = (e) => { gotoStage(e.target.value); e.target.value = ""; };
$("panel-max").onclick = toggleMaximize;
$("font-inc").onclick = () => bumpFont(1);
$("font-dec").onclick = () => bumpFont(-1);
$("project").onchange = onProjectFilter;
$("add-project").onclick = addProject;
$("new-task").onclick = openTaskDialog;
$("task-cancel").onclick = () => $("task-dialog").close();
$("task-create").onclick = createTask;
$("task-gh-load").onclick = loadGithubIssues;
$("task-project").onchange = () => setTaskTab(state.taskTracker);
document.querySelectorAll(".task-tabs button").forEach((b) => { b.onclick = () => setTaskTab(b.dataset.tracker); });
$("panel-edit-text").onclick = openTextEditor;
$("panel-file-issue").onclick = fileAsIssue;
$("text-cancel").onclick = () => $("text-dialog").close();
$("text-save").onclick = saveTaskText;
$("theme-toggle").onclick = toggleTheme;
$("settings-gear").onclick = openSettings;
$("settings-close").onclick = closeSettings;
$("settings-overlay").onclick = closeSettings;
document.querySelectorAll(".settings-nav-item").forEach((b) =>
  b.addEventListener("click", () => switchSettingsSection(b.dataset.section)));
// Show the first section by default
switchSettingsSection("models");
// Enter sends; Shift+Enter inserts a newline. Grow on every input and when the window resizes
// (a narrower box wraps more lines → needs more height).
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$("chat-input").addEventListener("input", autoGrowChat);
window.addEventListener("resize", autoGrowChat);

applyTheme(localStorage.getItem("sm-theme") || "light");
applyFontSize();
applyTranscriptHeight();
// Persist the transcript height whenever the user drags its resize handle (skip while maximized,
// where the height is flex-driven, not a real preference).
new ResizeObserver(() => {
  if (!document.body.classList.contains("workspace-max")) {
    const h = $("transcript").style.height;
    if (h && h !== "auto") localStorage.setItem("sm-transcript-h", h);
  }
}).observe($("transcript"));
// Esc exits full-screen (works even if the button scrolled out of view).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("workspace-max")) setMaximized(false);
});
loadProjects();
// Populate the go-to-stage select from the server's stage list (retried on settings open).
fetchAppConfig().then((cfg) => renderGotoStageOptions(cfg.stages || [])).catch(() => {});
refreshStatus();
setInterval(refreshStatus, 3000);
