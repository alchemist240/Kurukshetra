import asyncio
from backend.agents.base import ArenaAgent
from backend.config import settings
from backend.core.events import EventBus
from backend.core.registry import AgentRegistry


class ArenaOrchestrator:
    """Runs zero-prompt wake-ups and autonomous multi-agent debates."""

    def __init__(self, registry: AgentRegistry, event_bus: EventBus):
        self.registry = registry
        self.event_bus = event_bus

        self.topic: str = ""
        self.history: list[dict] = []
        self.is_running: bool = False

        # Traffic-light controls for Pause / Resume / Stop / Mid-debate Nudges
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # Green light by default
        self._stop_requested: bool = False
        self._pending_nudges: list[str] = []

    # --- Live Controls (called from WebSocket UI buttons) ---

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def stop(self) -> None:
        self._stop_requested = True
        self._resume_event.set()  # Unblock immediately if currently paused

    def add_user_nudge(self, text: str) -> None:
        if text.strip():
            self._pending_nudges.append(text.strip())

    # --- Mode 1: Zero-Prompt Self-Invoke Wake-Up ---

    async def wake_up_arena(self) -> None:
        """All agents wake up simultaneously, discover peers, pick an opening stance, and greet you."""
        self.registry.reset_stances()
        agents = self.registry.all_agents()
        peers = self.registry.get_peer_roster()

        await self.event_bus.publish("roster_update", {"agents": peers})

        wake_prompt = (
            'You just booted up inside the Kurukshetra Arena. Your creator "Kshitij" just opened the console. '
            "Introduce yourself briefly to Kshitij, acknowledge the other rival models sitting in the room with you, "
            "and pick your opening stance."
        )

        async def _greet(agent: ArenaAgent):
            await self.event_bus.publish("agent_thinking", {"agent_id": agent.agent_id, "agent_name": agent.name})
            try:
                res = await agent.act(wake_prompt, peers)
                payload = res.model_dump(mode="json")
                payload["phase"] = "wakeup"
                await self.event_bus.publish("agent_message", payload)
                await self.event_bus.publish("roster_update", {"agents": self.registry.get_peer_roster()})
            except Exception as e:
                await self.event_bus.publish("agent_error", {
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "error": str(e),
                })

        await asyncio.gather(*[_greet(a) for a in agents])
        await self.event_bus.publish("arena_ready", {"message": "All warriors are awake. Drop a topic to start."})

    # --- Mode 2: Autonomous Self-Invoking Debate Loop ---

    async def run_debate(
        self,
        topic: str,
        agent_ids: list[str] | None = None,
        max_turns: int | None = None,
    ) -> None:
        """Agents debate autonomously turn-by-turn until consensus, turn limit, or manual stop."""
        if self.is_running:
            await self.event_bus.publish("system_notice", {"message": "A debate is already running."})
            return

        active_agents = self.registry.get_selected(agent_ids)
        if len(active_agents) < 2:
            await self.event_bus.publish("system_notice", {"message": "Select at least 2 agents to start a debate."})
            return

        self.is_running = True
        self._stop_requested = False
        self._resume_event.set()
        self._pending_nudges.clear()
        self.topic = topic.strip()
        self.history = []

        limit = max_turns or settings.max_debate_turns
        consensus_votes: dict[str, bool] = {a.agent_id: False for a in active_agents}
        end_reason = "max_turns_reached"

        self.registry.reset_stances([a.agent_id for a in active_agents])
        await self.event_bus.publish("roster_update", {"agents": self.registry.get_peer_roster()})

        await self.event_bus.publish("debate_started", {
            "topic": self.topic,
            "max_turns": limit,
            "agents": self.registry.get_peer_roster([a.agent_id for a in active_agents]),
        })

        try:
            for turn in range(1, limit + 1):
                if self._stop_requested:
                    end_reason = "stopped_by_user"
                    break

                # If paused, wait cleanly here until Resume or Stop is clicked
                if not self._resume_event.is_set():
                    await self.event_bus.publish("debate_paused", {"turn": turn})
                    await self._resume_event.wait()
                    if self._stop_requested:
                        end_reason = "stopped_by_user"
                        break
                    await self.event_bus.publish("debate_resumed", {"turn": turn})

                # Inject any mid-debate nudges Kshitij typed while watching
                while self._pending_nudges:
                    nudge = self._pending_nudges.pop(0)
                    self.history.append({"speaker": "Kshitij (Creator)", "stance": "Directive", "message": nudge})
                    # A new human twist resets consensus so agents must react to it
                    consensus_votes = {a.agent_id: False for a in active_agents}
                    await self.event_bus.publish("user_interjection", {"message": nudge})

                agent = active_agents[(turn - 1) % len(active_agents)]
                peers = self.registry.get_peer_roster([a.agent_id for a in active_agents])

                await self.event_bus.publish("agent_thinking", {
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "turn": turn,
                })

                turn_prompt = self._build_turn_prompt(turn, limit, len(active_agents))

                try:
                    res = await agent.act(turn_prompt, peers)
                except Exception as e:
                    await self.event_bus.publish("agent_error", {
                        "agent_id": agent.agent_id,
                        "agent_name": agent.name,
                        "turn": turn,
                        "error": str(e),
                    })
                    continue

                # Record turn and broadcast live to UI (and Phase 2 subscribers)
                self.history.append({
                    "turn": turn,
                    "speaker": res.agent_name,
                    "stance": res.stance.value,
                    "message": res.message,
                })

                payload = res.model_dump(mode="json")
                payload["turn"] = turn
                payload["max_turns"] = limit
                payload["phase"] = "debate"

                await self.event_bus.publish("agent_message", payload)
                await self.event_bus.publish("roster_update", {"agents": self.registry.get_peer_roster()})

                # Track consensus ONLY after every warrior has spoken at least once
                if turn >= len(active_agents):
                    consensus_votes[agent.agent_id] = res.consensus_reached
                    if all(consensus_votes.values()):
                        end_reason = "consensus_reached"
                        break

                # Brief pacing pause so you can read live and hit Pause/Nudge comfortably
                await asyncio.sleep(0.8)

        finally:
            self.is_running = False
            await self.event_bus.publish("debate_ended", {
                "reason": end_reason,
                "total_turns": len([h for h in self.history if "turn" in h]),
                "topic": self.topic,
            })

    def _build_turn_prompt(self, turn: int, max_turns: int, num_agents: int) -> str:
        """Builds the live context each warrior sees before taking its turn."""
        if not self.history:
            return (
                f"DEBATE TOPIC (from Kshitij): {self.topic}\n"
                f"Turn {turn} of {max_turns}. You are opening the debate.\n"
                "Take a bold, clear opening stance and lay down your thesis for the other agents to challenge."
            )

        # Keep the last 10 turns so token cost stays low and focus stays razor sharp
        recent = self.history[-10:]
        transcript = "\n".join(
            f"- {entry['speaker']} [{entry['stance']}]: {entry['message']}"
            for entry in recent
        )

        consensus_hint = (
            "Do NOT set consensus_reached=true yet—everyone hasn't even had their opening turn."
            if turn <= num_agents
            else "If the group has genuinely converged on a strong, concrete conclusion, you may set consensus_reached=true. Otherwise keep challenging or synthesizing."
        )

        return (
            f"DEBATE TOPIC: {self.topic}\n"
            f"Turn {turn} of {max_turns}.\n\n"
            f"RECENT ARENA TRANSCRIPT:\n{transcript}\n\n"
            "INSTRUCTIONS:\n"
            "1. Directly address or challenge what the previous agent(s) just argued.\n"
            "2. Switch your stance dynamically if the room's tone demands it (e.g., use Devil's Advocate if everyone agrees too easily, "
            "Socratic Interrogator to expose holes, or Diplomatic Synthesizer to close a deadlock).\n"
            f"3. {consensus_hint}"
        )