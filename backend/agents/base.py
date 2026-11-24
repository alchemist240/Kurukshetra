from abc import ABC, abstractmethod
import asyncio
import json
import re
import time
from typing import Any
from pydantic import BaseModel

from backend.agents.stances import (
    Stance,
    format_stances_for_prompt,
    format_stance_choices,
    get_stance_color,
)


class AgentResponse(BaseModel):
    """Everything returned after an agent finishes one turn."""
    agent_id: str
    agent_name: str
    provider: str
    model: str
    stance: Stance
    stance_color: str
    reasoning: str
    message: str
    consensus_reached: bool = False
    needs_human_input: bool = False
    question_for_user: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class ArenaAgent(ABC):
    """Base brain shared by Groq, Gemini, OpenAI, and DeepSeek agents."""

    def __init__(self, agent_id: str, name: str, provider: str, model: str):
        self.agent_id = agent_id
        self.name = name
        self.provider = provider
        self.model = model
        self.current_stance: Stance | None = None

    def _build_system_prompt(self, peers: list[dict[str, str]]) -> str:
        peer_lines = "\n".join(
            f"- {p['name']} ({p['provider']} / {p['model']}) — current stance: {p['stance']}"
            for p in peers
            if p["agent_id"] != self.agent_id
        ) or "- None yet (you are first in the arena)"

        return f"""You are {self.name}, an autonomous AI warrior inside the 'Kurukshetra' Multi-Agent Arena.
Your identity: Provider = {self.provider}, Model = {self.model}.

OTHER AGENTS IN THE ARENA RIGHT NOW:
{peer_lines}

YOUR AVAILABLE POWERS (STANCES):
{format_stances_for_prompt()}

RULES:
1. On every turn, read the room and autonomously choose the stance that fits best right now.
   You may keep your stance or switch it based on how other agents just replied.
2. Briefly explain your private reasoning for picking that stance.
3. Keep your public message punchy, sharp, and under 90 words.
4. Set "consensus_reached" to true ONLY if a topic is being debated and you genuinely agree
   the group has reached a solid conclusion. (Always false during initial greetings.)
5. HUMAN CLARIFICATION: Set "needs_human_input" to true ONLY during an active debate when a
   critical personal detail from Kshitij (e.g., budget, exact weak points, constraints, goals)
   is genuinely missing and blocks a meaningful recommendation. Put ONE concise question in
   "question_for_user". Otherwise keep "needs_human_input": false and "question_for_user": "".
   (Always false during initial greetings.)

You MUST respond ONLY with valid JSON in this exact format:
{{
  "stance": "{format_stance_choices()}",
  "reasoning": "1-2 sentences on why you chose this stance right now",
  "message": "Your public message to the user or other agents",
  "consensus_reached": false,
  "needs_human_input": false,
  "question_for_user": ""
}}"""

    async def act(
        self,
        user_prompt: str,
        peers: list[dict[str, str]],
    ) -> AgentResponse:
        """Runs one full turn: builds prompt -> calls provider API -> parses decision."""
        system_prompt = self._build_system_prompt(peers)
        start = time.perf_counter()

        try:
            raw_text, prompt_tokens, completion_tokens = await asyncio.wait_for(
                self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                ),
                timeout=25.0,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"{self.name} ({self.model}) did not respond within 25 seconds.")

        latency_ms = int((time.perf_counter() - start) * 1000)

        data = self._parse_json(raw_text)
        chosen_stance = self._safe_stance(data.get("stance"))
        self.current_stance = chosen_stance

        needs_input = bool(data.get("needs_human_input", False))
        question = str(data.get("question_for_user", "")).strip() if needs_input else ""
        if not question:
            needs_input = False

        return AgentResponse(
            agent_id=self.agent_id,
            agent_name=self.name,
            provider=self.provider,
            model=self.model,
            stance=chosen_stance,
            stance_color=get_stance_color(chosen_stance),
            reasoning=str(data.get("reasoning", "Default tactical choice.")),
            message=str(data.get("message", raw_text)).strip(),
            consensus_reached=bool(data.get("consensus_reached", False)),
            needs_human_input=needs_input,
            question_for_user=question,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

    async def vote_on_clarification(
        self,
        topic: str,
        requester_name: str,
        question: str,
        recent_transcript: str,
    ) -> dict[str, Any]:
        """
        Lightweight parallel micro-vote: decides whether pausing to ask Kshitij
        is strictly necessary or if the room should just make an assumption.
        """
        vote_system = (
            f"You are {self.name} in the Kurukshetra debate arena. "
            "Another agent wants to pause the debate to ask the human user (Kshitij) a clarification question. "
            "Vote true ONLY if the answer is genuinely required to give a useful conclusion and cannot be "
            "reasonably assumed. Vote false if the question is trivial, already answered, or stalls the debate.\n"
            'Respond ONLY with valid JSON: {"vote": true, "reason": "max 15 words explaining your vote"}'
        )

        vote_user = (
            f"TOPIC: {topic}\n\n"
            f"RECENT TRANSCRIPT:\n{recent_transcript}\n\n"
            f"PROPOSAL BY {requester_name}: Pause and ask Kshitij -> \"{question}\"\n"
            "Do you vote YES (true) or NO (false)?"
        )

        try:
            raw_text, prompt_tokens, completion_tokens = await asyncio.wait_for(
                self._call_llm(system_prompt=vote_system, user_prompt=vote_user),
                timeout=15.0,
            )
            data = self._parse_json(raw_text)
            return {
                "agent_id": self.agent_id,
                "agent_name": self.name,
                "vote": bool(data.get("vote", False)),
                "reason": str(data.get("reason", "No reason given.")).strip(),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        except Exception as e:
            # If a voter times out or errors, count as NO so it never blocks the arena
            return {
                "agent_id": self.agent_id,
                "agent_name": self.name,
                "vote": False,
                "reason": f"Abstained ({type(e).__name__}).",
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

    @abstractmethod
    async def _call_llm(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        """Each provider implements ONLY this method. Returns (text, prompt_tokens, completion_tokens)."""

    @staticmethod
    def _parse_json(raw_text: str) -> dict[str, Any]:
        """Safely extracts JSON even if the model wraps it in ```json ... ``` blocks."""
        cleaned = raw_text.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        return {
            "stance": Stance.ASSERTIVE.value,
            "reasoning": "Fallback parse (model returned plain text).",
            "message": cleaned,
            "consensus_reached": False,
        }

    @staticmethod
    def _safe_stance(value: Any) -> Stance:
        for s in Stance:
            if str(value).strip().lower() == s.value.lower():
                return s
        return Stance.ASSERTIVE