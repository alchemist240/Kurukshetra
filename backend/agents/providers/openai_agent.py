from openai import AsyncOpenAI
from backend.agents.base import ArenaAgent
from backend.config import settings


class OpenAIAgent(ArenaAgent):
    def __init__(self):
        super().__init__(
            agent_id="openai",
            name="OpenAI",
            provider="OpenAI",
            model=settings.openai_model,
        )
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return text, (usage.prompt_tokens if usage else 0), (usage.completion_tokens if usage else 0)