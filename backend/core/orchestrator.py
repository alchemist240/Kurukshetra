import asyncio
from backend.agents.base import ArenaAgent
from backend.config import settings
from backend.core.events import EventBus
from backend.core.registry import AgentRegistry
from backend.storage.store import DebateStore

MAX_CLARIFICATIONS_PER_DEBATE = 2
VOTE_COOLDOWN_TURNS = 2


class ArenaOrchestrator:
    """Runs zero-prompt wake-ups and autonomous multi-agent debates."""

    def __init__(
        self,
        registry: AgentRegistry,
        event_bus: EventBus,
        store: DebateStore | None = None,
    ):
        self.registry = registry
        self.event_bus = event_bus
        self.store = store

        self.topic: str = ""
        self.current_debate_id: str | None = None
        self.history: list[dict] = []
        self.is_running: bool = False

        # Traffic-light #1: Manual Pause / Resume / Stop / Mid-debate Nudges
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # Green light by default
        self._stop_requested: bool = False
        self._pending_nudges: list[str] = []

        # Traffic-light #2: Majority-Vote Human Clarification Pause
        self._clarification_event = asyncio.Event()
        self._clarification_event.set()
        self.awaiting_clarification: bool = False
        self._clarification_answer: str = ""

    # --- Live Controls (called from WebSocket UI buttons) ---

    def pause(self) -> None:
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def stop(self) -> None:
        self._stop_requested = True
        self._resume_event.set()          # Unblock if manually paused
        self._clarification_event.set()   # Unblock if waiting for human clarification

    def add_user_nudge(self, text: str) -> None:
        if text.strip():
            self._pending_nudges.append(text.strip())

    def submit_clarification(self, answer: str) -> None:
        """Called when Kshitij answers (or skips) a majority-approved clarification question."""
        if not self.awaiting_clarification:
            return
        self._clarification_answer = answer.strip()
        self._clarification_event.set()

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
        self._clarification_event.set()
        self.awaiting_clarification = False
        self._pending_nudges.clear()
        self.topic = topic.strip()
        self.history = []

        limit = max_turns or settings.max_debate_turns
        consensus_votes: dict[str, bool] = {a.agent_id: False for a in active_agents}
        end_reason = "max_turns_reached"

        # Anti-spam tracking for human clarification votes
        clarifications_used = 0
        last_vote_turn = -VOTE_COOLDOWN_TURNS

        # Reset stances for a clean tactical slate
        self.registry.reset_stances([a.agent_id for a in active_agents])
        active_roster = self.registry.get_peer_roster([a.agent_id for a in active_agents])
        await self.event_bus.publish("roster_update", {"agents": self.registry.get_peer_roster()})

        # Create persistent debate record in Redis + local JSON
        debate_id: str | None = None
        if self.store:
            debate_id = await self.store.create_debate(
                topic=self.topic,
                agents=active_roster,
                max_turns=limit,
            )
        self.current_debate_id = debate_id

        await self.event_bus.publish("debate_started", {
            "debate_id": debate_id,
            "topic": self.topic,
            "max_turns": limit,
            "agents": active_roster,
        })

        try:
            for turn in range(1, limit + 1):
                if self._stop_requested:
                    end_reason = "stopped_by_user"
                    break

                # If manually paused, wait cleanly here until Resume or Stop is clicked
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
                    consensus_votes = {a.agent_id: False for a in active_agents}
                    nudge_payload = {"message": nudge}
                    await self.event_bus.publish("user_interjection", nudge_payload)
                    if self.store and debate_id:
                        await self.store.append_event(debate_id, "user_interjection", nudge_payload)

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
                    err_payload = {
                        "agent_id": agent.agent_id,
                        "agent_name": agent.name,
                        "turn": turn,
                        "error": str(e),
                    }
                    await self.event_bus.publish("agent_error", err_payload)
                    if self.store and debate_id:
                        await self.store.append_event(debate_id, "agent_error", err_payload)
                    continue

                # Record turn in RAM history
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

                # Persist turn to Redis + JSON and broadcast live to UI
                if self.store and debate_id:
                    await self.store.append_event(debate_id, "agent_message", payload)

                await self.event_bus.publish("agent_message", payload)
                await self.event_bus.publish("roster_update", {"agents": self.registry.get_peer_roster()})

                # --- Majority-Vote Human-in-the-Loop Clarification Check ---
                can_vote = (
                    res.needs_human_input
                    and bool(res.question_for_user)
                    and not self._stop_requested
                    and turn < limit
                    and clarifications_used < MAX_CLARIFICATIONS_PER_DEBATE
                    and (turn - last_vote_turn) >= VOTE_COOLDOWN_TURNS
                )

                if can_vote:
                    last_vote_turn = turn
                    vote_result = await self._run_clarification_vote(
                        requester=agent,
                        active_agents=active_agents,
                        question=res.question_for_user,
                        turn=turn,
                    )

                    if vote_result["approved"]:
                        await self.event_bus.publish("clarification_requested", vote_result)
                        if self.store and debate_id:
                            await self.store.append_event(debate_id, "clarification_requested", vote_result)

                        # Pause cleanly here until Kshitij replies, skips, or clicks Stop
                        self.awaiting_clarification = True
                        self._clarification_answer = ""
                        self._clarification_event.clear()
                        await self._clarification_event.wait()
                        self.awaiting_clarification = False

                        if self._stop_requested:
                            end_reason = "stopped_by_user"
                            break

                        clarifications_used += 1
                        skipped = not bool(self._clarification_answer)
                        final_answer = (
                            self._clarification_answer
                            if not skipped
                            else "Skipped by Kshitij — make reasonable assumptions and continue."
                        )

                        # Inject Kshitij's answer into history & reset consensus so everyone reacts
                        self.history.append({
                            "speaker": "Kshitij (Clarification)",
                            "stance": "Skipped" if skipped else "Clarification",
                            "message": f"Q ({agent.name}): {res.question_for_user} -> A: {final_answer}",
                        })
                        consensus_votes = {a.agent_id: False for a in active_agents}

                        ans_payload = {
                            "turn": turn,
                            "requester": agent.name,
                            "question": res.question_for_user,
                            "answer": final_answer,
                            "skipped": skipped,
                        }
                        await self.event_bus.publish("clarification_answered", ans_payload)
                        if self.store and debate_id:
                            await self.store.append_event(debate_id, "clarification_answered", ans_payload)

                        # Move directly to next turn with Kshitij's new context in hand
                        continue
                    else:
                        await self.event_bus.publish("clarification_rejected", vote_result)
                        if self.store and debate_id:
                            await self.store.append_event(debate_id, "clarification_rejected", vote_result)

                        self.history.append({
                            "speaker": "Arena System",
                            "stance": "Vote Overruled",
                            "message": (
                                f"{agent.name}'s request to pause and ask Kshitij "
                                f"('{res.question_for_user}') was overruled "
                                f"({vote_result['yes_votes']}/{vote_result['total_voters']} voted YES). "
                                "State a clear assumption and keep debating."
                            ),
                        })

                # Track consensus ONLY after every warrior has spoken at least once
                if turn >= len(active_agents):
                    consensus_votes[agent.agent_id] = res.consensus_reached
                    if all(consensus_votes.values()):
                        end_reason = "consensus_reached"
                        break

                await asyncio.sleep(0.8)

        finally:
            self.is_running = False
            self.awaiting_clarification = False
            completed_turns = len([h for h in self.history if "turn" in h])

            if self.store and debate_id:
                await self.store.finish_debate(
                    debate_id=debate_id,
                    end_reason=end_reason,
                    total_turns=completed_turns,
                )

            await self.event_bus.publish("debate_ended", {
                "debate_id": debate_id,
                "reason": end_reason,
                "total_turns": completed_turns,
                "topic": self.topic,
            })

    async def _run_clarification_vote(
        self,
        requester: ArenaAgent,
        active_agents: list[ArenaAgent],
        question: str,
        turn: int,
    ) -> dict:
        """Polls all other active warriors in parallel to decide if Kshitij should be paused."""
        await self.event_bus.publish("clarification_voting", {
            "turn": turn,
            "requester_id": requester.agent_id,
            "requester_name": requester.name,
            "question": question,
        })

        recent_transcript = self._format_recent_transcript(limit=6)
        other_agents = [a for a in active_agents if a.agent_id != requester.agent_id]

        # Fire all peer votes at the exact same moment (~1.5s total)
        peer_votes = await asyncio.gather(*[
            peer.vote_on_clarification(
                topic=self.topic,
                requester_name=requester.name,
                question=question,
                recent_transcript=recent_transcript,
            )
            for peer in other_agents
        ])

        # Requester automatically counts as 1 YES vote
        all_votes = [
            {
                "agent_id": requester.agent_id,
                "agent_name": requester.name,
                "vote": True,
                "reason": "Proposed this clarification question.",
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
            *peer_votes,
        ]

        total_voters = len(active_agents)
        required = (total_voters // 2) + 1  # 2->2, 3->2, 4->3
        yes_votes = sum(1 for v in all_votes if v["vote"])
        vote_tokens = sum(v["prompt_tokens"] + v["completion_tokens"] for v in all_votes)

        return {
            "turn": turn,
            "requester_id": requester.agent_id,
            "requester_name": requester.name,
            "question": question,
            "yes_votes": yes_votes,
            "total_voters": total_voters,
            "required_votes": required,
            "approved": yes_votes >= required,
            "vote_tokens": vote_tokens,
            "votes": all_votes,
        }

    def _format_recent_transcript(self, limit: int = 10) -> str:
        recent = self.history[-limit:]
        return "\n".join(
            f"- {entry['speaker']} [{entry['stance']}]: {entry['message']}"
            for entry in recent
        )

    def _build_turn_prompt(self, turn: int, max_turns: int, num_agents: int) -> str:
        """Builds the live context each warrior sees before taking its turn."""
        if not self.history:
            return (
                f"DEBATE TOPIC (from Kshitij): {self.topic}\n"
                f"Turn {turn} of {max_turns}. You are opening the debate.\n"
                "Take a bold, clear opening stance and lay down your thesis for the other agents to challenge."
            )

        transcript = self._format_recent_transcript(limit=10)

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
            f"3. {consensus_hint}\n"
            "4. Only set needs_human_input=true if a missing personal detail from Kshitij genuinely blocks the room from giving a tailored answer."
        )