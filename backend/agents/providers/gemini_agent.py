from google import genai
from google.genai import errors, types
from backend.agents.base import ArenaAgent
from backend.config import settings


class GeminiAgent(ArenaAgent):
    FALLBACK_MODELS = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]

    def __init__(self):
        super().__init__(
            agent_id="gemini",
            name="Gemini",
            provider="Google Gemini",
            model=settings.gemini_model,
        )
        # Fail fast (attempts=1, 15s timeout) so our fallback runs immediately instead of hanging
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=15_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        candidates = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        last_error = None

        for model_name in candidates:
            try:
                resp = await self.client.aio.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                    ),
                )
                self.model = model_name
                text = resp.text or ""
                meta = resp.usage_metadata
                prompt_tokens = (meta.prompt_token_count or 0) if meta else 0
                completion_tokens = (meta.candidates_token_count or 0) if meta else 0
                return text, prompt_tokens, completion_tokens
            except errors.APIError as e:
                # Fall back immediately on 503 (overloaded) or 404 (unavailable model)
                if getattr(e, "code", None) in (503, 404):
                    last_error = e
                    continue
                raise

        raise last_error