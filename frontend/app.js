// --- DOM References ---
const connBadge = document.getElementById("conn-badge");
const turnCounter = document.getElementById("turn-counter");
const tokenCounter = document.getElementById("token-counter");
const rosterList = document.getElementById("roster-list");
const feed = document.getElementById("feed");
const thinkingBar = document.getElementById("thinking-bar");
const topicBanner = document.getElementById("topic-banner");
const stageStatus = document.getElementById("stage-status");
const activeTopicText = document.getElementById("active-topic-text");

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
let selectedAgents = new Set(); // Remembers your checkbox picks across roster updates
let thinkingAgents = new Set(); // Tracks who is currently calling their API
let agentTokens = {};           // Cumulative tokens per warrior
let totalTokens = 0;
let isDebating = false;

// --- Helpers ---
function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function scrollFeedToBottom() {
  feed.scrollTop = feed.scrollHeight;
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

// --- Render Functions ---
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

  // Re-attach checkbox listeners
  document.querySelectorAll(".agent-checkbox").forEach((box) => {
    box.addEventListener("change", (e) => {
      const id = e.target.dataset.id;
      if (e.target.checked) selectedAgents.add(id);
      else selectedAgents.delete(id);
    });
  });
}

function updateThinkingBar() {
  if (thinkingAgents.size === 0) {
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

function appendAgentCard(d) {
  const isDebate = d.phase === "debate";
  const turnLabel = isDebate ? `Turn ${d.turn}/${d.max_turns}` : "WAKE-UP";
  const usedTokens = (d.prompt_tokens || 0) + (d.completion_tokens || 0);

  // Update live token stats
  agentTokens[d.agent_id] = (agentTokens[d.agent_id] || 0) + usedTokens;
  totalTokens += usedTokens;
  tokenCounter.textContent = `Tokens: ${totalTokens.toLocaleString()}`;
  if (isDebate) {
    turnCounter.textContent = `Turn: ${d.turn} / ${d.max_turns}`;
  }

  const card = document.createElement("article");
  card.className = "msg-card";
  card.style.borderLeftColor = d.stance_color || "#3b82f6";

  card.innerHTML = `
    <div class="msg-header">
      <div class="msg-title">
        <span class="agent-name">${escapeHtml(d.agent_name)}</span>
        <span class="model-tag">${escapeHtml(d.model)}</span>
        <span class="stance-badge" style="background: ${d.stance_color}">${escapeHtml(d.stance)}</span>
        <span class="turn-tag">${escapeHtml(turnLabel)}</span>
        ${d.consensus_reached ? `<span class="consensus-tag">✓ AGREED</span>` : ""}
      </div>
      <div class="msg-metrics">${d.latency_ms}ms · ${usedTokens} tok</div>
    </div>
    <div class="reasoning-box">
      <span class="reasoning-label">Tactical Reasoning:</span>
      ${escapeHtml(d.reasoning)}
    </div>
    <div class="msg-body">${escapeHtml(d.message)}</div>
  `;

  feed.appendChild(card);
  scrollFeedToBottom();
}

function appendSystemCard(text, extraClass = "") {
  const div = document.createElement("div");
  div.className = `system-card ${extraClass}`.trim();
  div.textContent = text;
  feed.appendChild(div);
  scrollFeedToBottom();
}

// --- WebSocket Connection & Event Router ---
function connectWebSocket() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${window.location.host}/ws/arena`);

  ws.onopen = () => {
    connBadge.textContent = "Live Connected";
    connBadge.className = "pill pill-online";
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
        // First load: select all warriors by default
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
        setControlsState({ debating: true, paused: false });
        topicBanner.classList.remove("hidden");
        stageStatus.textContent = "LIVE";
        stageStatus.style.background = "#3b82f6";
        activeTopicText.textContent = data.topic;
        turnCounter.textContent = `Turn: 0 / ${data.max_turns}`;
        appendSystemCard(`🔥 Debate started: "${data.topic}" (${data.agents.map((a) => a.name).join(" vs ")})`);
        break;

      case "debate_paused":
        setControlsState({ debating: true, paused: true });
        stageStatus.textContent = "PAUSED";
        stageStatus.style.background = "#ca8a04";
        appendSystemCard(`⏸️ Debate paused before Turn ${data.turn}.`);
        break;

      case "debate_resumed":
        setControlsState({ debating: true, paused: false });
        stageStatus.textContent = "LIVE";
        stageStatus.style.background = "#3b82f6";
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
        stageStatus.textContent = label;
        stageStatus.style.background = data.reason === "consensus_reached" ? "#16a34a" : "#475569";
        appendSystemCard(`🏁 Debate ended — ${label} (${data.total_turns} turns).`);
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
btnRewake.addEventListener("click", () => sendCommand({ action: "wake_up" }));

nudgeForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = nudgeInput.value.trim();
  if (!msg || !isDebating) return;
  sendCommand({ action: "nudge", message: msg });
  nudgeInput.value = "";
});

// Boot WebSocket immediately on page load
connectWebSocket();