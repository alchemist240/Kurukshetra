import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Loads variables from your .env file into Python's environment
load_dotenv()


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")

    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-6-luna")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    max_debate_turns: int = int(os.getenv("MAX_DEBATE_TURNS", "12"))
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")


settings = Settings()