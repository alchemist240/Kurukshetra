from abc import ABC, abstractmethod
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

You MUST respond ONLY with valid JSON in this exact format:
{{
  "stance": "{format_stance_choices()}",
  "reasoning": "2-3 sentences on why you chose this stance right now",
  "message": "Your public message to the user or other agents",
  "consensus_reached": false
}}"""

    async def act(
        self,
        user_prompt: str,
        peers: list[dict[str, str]],
    ) -> AgentResponse:
        """Runs one full turn: builds prompt -> calls provider API -> parses decision."""
        system_prompt = self._build_system_prompt(peers)
        start = time.perf_counter()

        raw_text, prompt_tokens, completion_tokens = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        data = self._parse_json(raw_text)
        chosen_stance = self._safe_stance(data.get("stance"))
        self.current_stance = chosen_stance

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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

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