// --- DOM References ---
const connBadge = document.getElementById("conn-badge");
const storageBadge = document.getElementById("storage-badge");
const turnCounter = document.getElementById("turn-counter");
const tokenCounter = document.getElementById("token-counter");
const rosterList = document.getElementById("roster-list");
const historyList = document.getElementById("history-list");
const btnRefreshHistory = document.getElementById("btn-refresh-history");

const feed = document.getElementById("feed");
const replayFeed = document.getElementById("replay-feed");
const thinkingBar = document.getElementById("thinking-bar");
const topicBanner = document.getElementById("topic-banner");
const stageStatus = document.getElementById("stage-status");
const activeTopicText = document.getElementById("active-topic-text");
const btnBackLive = document.getElementById("btn-back-live");

const topicInput = document.getElementById("topic-input");
const maxTurnsInput = document.getElementById("max-turns-input");
const btnStart = document.getElementById("btn-start");
const btnPause = document.getElementById("btn-pause");
const btnResume = document.getElementById("btn-resume");
const btnStop = document.getElementById("btn-stop");
const btnRewake = document.getElementById("btn-rewake");
const nudgeForm = document.getElementById("nudge-form");
const nudgeInput = document.getElementById("nudge-input");
const btnNudge = document.getElementById("btn-nudge");

// --- Frontend State ---
let ws = null;
let latestRoster = [];
let selectedAgents = new Set();
let thinkingAgents = new Set();
let agentTokens = {};
let totalTokens = 0;
let isDebating = false;

// Live banner snapshot so we can restore it cleanly after closing a replay
let liveBannerState = { visible: false, statusText: "LIVE", statusBg: "#3b82f6", topic: "" };
let viewingReplayId = null;

// --- Helpers ---
function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function scrollToBottom(el) {
  el.scrollTop = el.scrollHeight;
}

function sendCommand(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

function setControlsState({ debating, paused = false }) {
  isDebating = debating;
  btnStart.disabled = debating;
  btnRewake.disabled = debating;
  btnPause.disabled = !debating || paused;
  btnResume.disabled = !debating || !paused;
  btnStop.disabled = !debating;
  nudgeInput.disabled = !debating;
  btnNudge.disabled = !debating;
}

function applyBanner({ visible, statusText, statusBg, topic, showBackBtn = false }) {
  if (!visible) {
    topicBanner.classList.add("hidden");
    return;
  }
  topicBanner.classList.remove("hidden");
  stageStatus.textContent = statusText;
  stageStatus.style.background = statusBg;
  activeTopicText.textContent = topic;
  btnBackLive.classList.toggle("hidden", !showBackBtn);
}

function updateLiveBanner(patch) {
  liveBannerState = { ...liveBannerState, ...patch };
  if (!viewingReplayId) {
    applyBanner({ ...liveBannerState, showBackBtn: false });
  }
}

// --- Shared Card Builders (used by BOTH Live Feed and Replay Feed) ---
function buildAgentCardElement(d) {
  const isDebate = d.phase === "debate" || d.turn !== undefined;
  const turnLabel = isDebate
    ? (d.max_turns ? `Turn ${d.turn}/${d.max_turns}` : `Turn ${d.turn}`)
    : "WAKE-UP";
  const usedTokens = (d.prompt_tokens || 0) + (d.completion_tokens || 0);
  const color = d.stance_color || "#3b82f6";

  const card = document.createElement("article");
  card.className = "msg-card";
  card.style.borderLeftColor = color;

  const modelHtml = d.model ? `<span class="model-tag">${escapeHtml(d.model)}</span>` : "";
  const reasoningHtml = d.reasoning
    ? `<div class="reasoning-box"><span class="reasoning-label">Tactical Reasoning:</span> ${escapeHtml(d.reasoning)}</div>`
    : "";
  const latencyText = d.latency_ms ? `${d.latency_ms}ms · ` : "";

  card.innerHTML = `
    <div class="msg-header">
      <div class="msg-title">
        <span class="agent-name">${escapeHtml(d.agent_name || d.agent_id)}</span>
        ${modelHtml}
        <span class="stance-badge" style="background: ${color}">${escapeHtml(d.stance || "Assertive")}</span>
        <span class="turn-tag">${escapeHtml(turnLabel)}</span>
        ${d.consensus_reached ? `<span class="consensus-tag">✓ AGREED</span>` : ""}
      </div>
      <div class="msg-metrics">${latencyText}${usedTokens} tok</div>
    </div>
    ${reasoningHtml}
    <div class="msg-body">${escapeHtml(d.message)}</div>
  `;
  return card;
}

function buildSystemCardElement(text, extraClass = "") {
  const div = document.createElement("div");
  div.className = `system-card ${extraClass}`.trim();
  div.textContent = text;
  return div;
}

function appendAgentCard(d) {
  const isDebate = d.phase === "debate";
  const usedTokens = (d.prompt_tokens || 0) + (d.completion_tokens || 0);

  agentTokens[d.agent_id] = (agentTokens[d.agent_id] || 0) + usedTokens;
  totalTokens += usedTokens;
  tokenCounter.textContent = `Tokens: ${totalTokens.toLocaleString()}`;
  if (isDebate) {
    turnCounter.textContent = `Turn: ${d.turn} / ${d.max_turns}`;
  }

  feed.appendChild(buildAgentCardElement(d));
  scrollToBottom(feed);
}

function appendSystemCard(text, extraClass = "") {
  feed.appendChild(buildSystemCardElement(text, extraClass));
  scrollToBottom(feed);
}

// --- Roster & Thinking Renderers ---
function renderRoster() {
  rosterList.innerHTML = latestRoster
    .map((a) => {
      const checked = selectedAgents.has(a.agent_id) ? "checked" : "";
      const isThinking = thinkingAgents.has(a.agent_id);
      const tok = agentTokens[a.agent_id] || 0;

      return `
        <div class="warrior-card ${isThinking ? "thinking" : ""}" style="border-left-color: ${a.stance_color}">
          <div class="warrior-top">
            <label class="warrior-check">
              <input type="checkbox" class="agent-checkbox" data-id="${escapeHtml(a.agent_id)}" ${checked} />
              <span>${escapeHtml(a.name)}</span>
            </label>
            ${isThinking ? `<span class="thinking-dot">thinking...</span>` : ""}
          </div>
          <div class="warrior-model">${escapeHtml(a.provider)} · ${escapeHtml(a.model)}</div>
          <div class="warrior-bottom">
            <span class="stance-badge" style="background: ${a.stance_color}">${escapeHtml(a.stance)}</span>
            <span class="mini-stat">${tok.toLocaleString()} tok</span>
          </div>
        </div>
      `;
    })
    .join("");

  document.querySelectorAll(".agent-checkbox").forEach((box) => {
    box.addEventListener("change", (e) => {
      const id = e.target.dataset.id;
      if (e.target.checked) selectedAgents.add(id);
      else selectedAgents.delete(id);
    });
  });
}

function updateThinkingBar() {
  if (thinkingAgents.size === 0 || viewingReplayId) {
    thinkingBar.classList.add("hidden");
    thinkingBar.textContent = "";
    return;
  }
  const names = latestRoster
    .filter((a) => thinkingAgents.has(a.agent_id))
    .map((a) => a.name);
  thinkingBar.textContent = `⚡ ${names.join(", ")} ${names.length > 1 ? "are" : "is"} thinking...`;
  thinkingBar.classList.remove("hidden");
}

// --- History & Replay Functions ---
async function loadHistory() {
  try {
    const resp = await fetch("/api/debates?limit=25");
    if (!resp.ok) return;
    const payload = await resp.json();

    // Update topbar storage indicator
    if (payload.redis_available) {
      storageBadge.textContent = "Redis + JSON";
      storageBadge.className = "pill pill-redis";
    } else {
      storageBadge.textContent = "JSON Only";
      storageBadge.className = "pill";
    }

    const debates = payload.debates || [];
    if (debates.length === 0) {
      historyList.innerHTML = `<div class="history-empty">No saved debates yet.</div>`;
      return;
    }

    historyList.innerHTML = debates
      .map((d) => {
        const activeClass = d.debate_id === viewingReplayId ? "active" : "";
        const srcClass = d.source === "json" ? "json" : "";
        const reasonTag = d.end_reason === "consensus_reached" ? "✓ Consensus" : (d.status === "running" ? "● Live" : `${d.total_turns}t`);

        return `
          <div class="history-item ${activeClass}" data-id="${escapeHtml(d.debate_id)}">
            <div class="history-topic">${escapeHtml(d.topic)}</div>
            <div class="history-meta">
              <span>${reasonTag} · ${Number(d.total_tokens || 0).toLocaleString()} tok</span>
              <span class="source-tag ${srcClass}">${escapeHtml(d.source || "json")}</span>
            </div>
          </div>
        `;
      })
      .join("");

    document.querySelectorAll(".history-item").forEach((el) => {
      el.addEventListener("click", () => openReplay(el.dataset.id));
    });
  } catch (err) {
    historyList.innerHTML = `<div class="history-empty">Failed to load history.</div>`;
  }
}

async function openReplay(debateId) {
  try {
    const resp = await fetch(`/api/debates/${encodeURIComponent(debateId)}`);
    if (!resp.ok) return;
    const doc = await resp.json();
    const meta = doc.meta || {};
    const events = doc.events || [];

    viewingReplayId = debateId;
    replayFeed.innerHTML = "";

    const agentNames = (meta.agents || []).map((a) => a.name || a.agent_id).join(" vs ");
    replayFeed.appendChild(
      buildSystemCardElement(
        `📼 Replay loaded from ${String(doc.source || "json").toUpperCase()} — ${agentNames || "Arena"} · ${meta.total_turns || 0} turns · ${Number(meta.total_tokens || 0).toLocaleString()} tokens`
      )
    );

    for (const ev of events) {
      if (ev.type === "agent_message") {
        replayFeed.appendChild(buildAgentCardElement(ev.data));
      } else if (ev.type === "user_interjection") {
        replayFeed.appendChild(
          buildSystemCardElement(`👑 Kshitij (Creator Interjection): ${ev.data.message}`, "nudge-card")
        );
      } else if (ev.type === "agent_error") {
        replayFeed.appendChild(
          buildSystemCardElement(`⚠️ ${ev.data.agent_name} error: ${ev.data.error}`, "error-card")
        );
      }
    }

    if (meta.end_reason) {
      replayFeed.appendChild(
        buildSystemCardElement(`🏁 Debate ended — ${meta.end_reason.replaceAll("_", " ").toUpperCase()}`)
      );
    }

    // Swap visible stage from Live -> Replay
    feed.classList.add("hidden");
    replayFeed.classList.remove("hidden");
    thinkingBar.classList.add("hidden");

    applyBanner({
      visible: true,
      statusText: `REPLAY (${String(doc.source || "json").toUpperCase()})`,
      statusBg: "#0891b2",
      topic: meta.topic || debateId,
      showBackBtn: true,
    });

    replayFeed.scrollTop = 0;
    loadHistory();
  } catch (err) {
    appendSystemCard("⚠️ Could not load saved debate.", "error-card");
  }
}

function exitReplay() {
  viewingReplayId = null;
  replayFeed.classList.add("hidden");
  feed.classList.remove("hidden");
  applyBanner({ ...liveBannerState, showBackBtn: false });
  updateThinkingBar();
  scrollToBottom(feed);
  loadHistory();
}

// --- WebSocket Connection & Event Router ---
function connectWebSocket() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${window.location.host}/ws/arena`);

  ws.onopen = () => {
    connBadge.textContent = "Live Connected";
    connBadge.className = "pill pill-online";
    loadHistory();
  };

  ws.onclose = () => {
    connBadge.textContent = "Reconnecting...";
    connBadge.className = "pill pill-offline";
    setControlsState({ debating: false });
    setTimeout(connectWebSocket, 2000);
  };

  ws.onmessage = (event) => {
    const { type, data } = JSON.parse(event.data);

    switch (type) {
      case "roster_update":
        latestRoster = data.agents || [];
        if (selectedAgents.size === 0) {
          latestRoster.forEach((a) => selectedAgents.add(a.agent_id));
        }
        renderRoster();
        break;

      case "agent_thinking":
        thinkingAgents.add(data.agent_id);
        renderRoster();
        updateThinkingBar();
        break;

      case "agent_message":
        thinkingAgents.delete(data.agent_id);
        appendAgentCard(data);
        renderRoster();
        updateThinkingBar();
        break;

      case "agent_error":
        thinkingAgents.delete(data.agent_id);
        appendSystemCard(`⚠️ ${data.agent_name} error: ${data.error}`, "error-card");
        renderRoster();
        updateThinkingBar();
        break;

      case "arena_ready":
        appendSystemCard(`⚔️ ${data.message}`);
        break;

      case "debate_started":
        if (viewingReplayId) exitReplay();
        setControlsState({ debating: true, paused: false });
        updateLiveBanner({
          visible: true,
          statusText: "LIVE",
          statusBg: "#3b82f6",
          topic: data.topic,
        });
        turnCounter.textContent = `Turn: 0 / ${data.max_turns}`;
        appendSystemCard(`🔥 Debate started: "${data.topic}" (${data.agents.map((a) => a.name).join(" vs ")})`);
        loadHistory();
        break;

      case "debate_paused":
        setControlsState({ debating: true, paused: true });
        updateLiveBanner({ statusText: "PAUSED", statusBg: "#ca8a04" });
        appendSystemCard(`⏸️ Debate paused before Turn ${data.turn}.`);
        break;

      case "debate_resumed":
        setControlsState({ debating: true, paused: false });
        updateLiveBanner({ statusText: "LIVE", statusBg: "#3b82f6" });
        appendSystemCard(`▶️ Debate resumed at Turn ${data.turn}.`);
        break;

      case "user_interjection":
        appendSystemCard(`👑 Kshitij (Creator Interjection): ${data.message}`, "nudge-card");
        break;

      case "debate_ended": {
        setControlsState({ debating: false });
        thinkingAgents.clear();
        updateThinkingBar();
        renderRoster();

        const labels = {
          consensus_reached: "CONSENSUS REACHED",
          max_turns_reached: "TURN LIMIT REACHED",
          stopped_by_user: "STOPPED BY CREATOR",
        };
        const label = labels[data.reason] || data.reason;
        updateLiveBanner({
          statusText: label,
          statusBg: data.reason === "consensus_reached" ? "#16a34a" : "#475569",
        });
        appendSystemCard(`🏁 Debate ended — ${label} (${data.total_turns} turns). Saved to Redis & JSON.`);
        loadHistory();
        break;
      }

      case "system_notice":
        appendSystemCard(`ℹ️ ${data.message}`);
        break;
    }
  };
}

// --- UI Button Listeners ---
btnStart.addEventListener("click", () => {
  const topic = topicInput.value.trim();
  const agentIds = Array.from(selectedAgents);
  const maxTurns = parseInt(maxTurnsInput.value, 10) || 12;

  if (!topic) {
    appendSystemCard("ℹ️ Enter a topic first before starting a debate.");
    topicInput.focus();
    return;
  }
  if (agentIds.length < 2) {
    appendSystemCard("ℹ️ Check at least 2 warriors in the sidebar to start.");
    return;
  }

  sendCommand({
    action: "start_debate",
    topic,
    agent_ids: agentIds,
    max_turns: maxTurns,
  });
});

btnPause.addEventListener("click", () => sendCommand({ action: "pause" }));
btnResume.addEventListener("click", () => sendCommand({ action: "resume" }));
btnStop.addEventListener("click", () => sendCommand({ action: "stop" }));
btnRewake.addEventListener("click", () => {
  if (viewingReplayId) exitReplay();
  sendCommand({ action: "wake_up" });
});
btnBackLive.addEventListener("click", exitReplay);
btnRefreshHistory.addEventListener("click", loadHistory);

nudgeForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = nudgeInput.value.trim();
  if (!msg || !isDebating) return;
  sendCommand({ action: "nudge", message: msg });
  nudgeInput.value = "";
});

// Boot WebSocket & History immediately
connectWebSocket();