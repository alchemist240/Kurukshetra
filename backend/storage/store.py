from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
import uuid
import redis.asyncio as redis

from backend.config import settings

# Where permanent JSON transcripts live on disk
DEBATES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "debates"
REDIS_TTL_SECONDS = 60 * 60 * 24 * 3  # Keep hot copies in Redis RAM for 3 days


class DebateStore:
    """
    Hybrid persistence engine:
      - Redis: fast live session state (Hashes + Lists + TTL)
      - Local JSON: permanent, human-readable audit trail in data/debates/
    Gracefully falls back to JSON-only if Redis/Docker is not running.
    """

    def __init__(self, redis_url: str | None = None, debates_dir: Path | None = None):
        self.redis_url = redis_url or settings.redis_url
        self.debates_dir = debates_dir or DEBATES_DIR
        self.debates_dir.mkdir(parents=True, exist_ok=True)

        self._redis: redis.Redis | None = None
        self.redis_available: bool = False

    # --- Lifecycle (Connect / Disconnect) ---

    async def connect(self) -> bool:
        """Tests connection to Redis once at startup. Safe to call even if Docker is off."""
        try:
            # decode_responses=True makes Redis return normal Python strings instead of b'bytes'
            client = redis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            self.redis_available = True
            print("[Storage] Connected to Redis + Local JSON archive.")
            return True
        except Exception as e:
            self._redis = None
            self.redis_available = False
            print(f"[Storage] Redis unavailable ({e}). Running in Local JSON-only mode.")
            return False

    async def close(self) -> None:
        """Cleanly releases Redis connection pool on server shutdown."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self.redis_available = False

    # --- Key & File Helpers ---

    @staticmethod
    def _meta_key(debate_id: str) -> str:
        return f"kurukshetra:debate:{debate_id}:meta"

    @staticmethod
    def _events_key(debate_id: str) -> str:
        return f"kurukshetra:debate:{debate_id}:events"

    @staticmethod
    def _index_key() -> str:
        return "kurukshetra:debates:index"

    def _json_path(self, debate_id: str) -> Path:
        # Path(debate_id).name prevents accidental folder traversal
        safe_id = Path(debate_id).name
        return self.debates_dir / f"{safe_id}.json"

    @staticmethod
    def _make_debate_id(topic: str) -> str:
        """Creates a clean, sortable ID like '20261124_143012_gym-split_a9f2'."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:28] or "debate"
        short_uid = uuid.uuid4().hex[:4]
        return f"{ts}_{slug}_{short_uid}"

    # --- Write Operations ---

    async def create_debate(
        self,
        topic: str,
        agents: list[dict[str, Any]],
        max_turns: int,
    ) -> str:
        """Creates a brand-new debate record in both Redis and data/debates/{id}.json."""
        debate_id = self._make_debate_id(topic)
        now = datetime.now(timezone.utc).isoformat()

        meta = {
            "debate_id": debate_id,
            "topic": topic,
            "status": "running",
            "max_turns": max_turns,
            "total_turns": 0,
            "total_tokens": 0,
            "end_reason": "",
            "started_at": now,
            "ended_at": "",
            "agents": agents,
        }

        # 1. Save permanent JSON skeleton on disk
        doc = {"meta": meta, "events": []}
        self._json_path(debate_id).write_text(json.dumps(doc, indent=2), encoding="utf-8")

        # 2. Save live metadata Hash + push ID to recent index in Redis
        if self.redis_available and self._redis:
            try:
                redis_meta = {
                    **meta,
                    "max_turns": str(max_turns),
                    "total_turns": "0",
                    "total_tokens": "0",
                    "agents": json.dumps(agents),  # Redis Hashes store flat strings
                }
                async with self._redis.pipeline(transaction=True) as pipe:
                    pipe.hset(self._meta_key(debate_id), mapping=redis_meta)
                    pipe.expire(self._meta_key(debate_id), REDIS_TTL_SECONDS)
                    pipe.lpush(self._index_key(), debate_id)
                    pipe.ltrim(self._index_key(), 0, 49)  # Keep latest 50 IDs in Redis index
                    await pipe.execute()
            except Exception as e:
                print(f"[Storage] Redis write warning (create_debate): {e}")

        return debate_id

    async def append_event(
        self,
        debate_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Records a turn, clarification vote/answer, interjection, or error to Redis AND JSON."""
        record = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

        # Count tokens from both regular agent turns AND parallel clarification votes
        added_tokens = 0
        if event_type == "agent_message":
            added_tokens = int(data.get("prompt_tokens", 0)) + int(data.get("completion_tokens", 0))
        elif event_type in ("clarification_requested", "clarification_rejected"):
            added_tokens = int(data.get("vote_tokens", 0))

        is_debate_turn = event_type == "agent_message" and data.get("phase") == "debate"

        # 1. Update local JSON file immediately (safe even if server crashes mid-debate)
        path = self._json_path(debate_id)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["events"].append(record)
            if added_tokens:
                doc["meta"]["total_tokens"] = int(doc["meta"].get("total_tokens", 0)) + added_tokens
            if is_debate_turn:
                doc["meta"]["total_turns"] = int(doc["meta"].get("total_turns", 0)) + 1
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        # 2. Push event to Redis List + atomically bump counters in Redis Hash
        if self.redis_available and self._redis:
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    pipe.rpush(self._events_key(debate_id), json.dumps(record))
                    pipe.expire(self._events_key(debate_id), REDIS_TTL_SECONDS)
                    if added_tokens:
                        pipe.hincrby(self._meta_key(debate_id), "total_tokens", added_tokens)
                    if is_debate_turn:
                        pipe.hincrby(self._meta_key(debate_id), "total_turns", 1)
                    await pipe.execute()
            except Exception as e:
                print(f"[Storage] Redis write warning (append_event): {e}")

    async def finish_debate(
        self,
        debate_id: str,
        end_reason: str,
        total_turns: int,
    ) -> None:
        """Marks the debate finished with final status, reason, and timestamp."""
        now = datetime.now(timezone.utc).isoformat()

        # 1. Finalize local JSON file
        path = self._json_path(debate_id)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["meta"]["status"] = "ended"
            doc["meta"]["end_reason"] = end_reason
            doc["meta"]["total_turns"] = total_turns
            doc["meta"]["ended_at"] = now
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        # 2. Finalize Redis Hash
        if self.redis_available and self._redis:
            try:
                await self._redis.hset(
                    self._meta_key(debate_id),
                    mapping={
                        "status": "ended",
                        "end_reason": end_reason,
                        "total_turns": str(total_turns),
                        "ended_at": now,
                    },
                )
            except Exception as e:
                print(f"[Storage] Redis write warning (finish_debate): {e}")

    # --- Read Operations (for History Sidebar & Future Phase 2 Replay) ---

    async def get_debate(self, debate_id: str) -> dict[str, Any] | None:
        """Loads a full debate: tries Redis RAM first, falls back to local JSON."""
        if self.redis_available and self._redis:
            try:
                raw_meta = await self._redis.hgetall(self._meta_key(debate_id))
                if raw_meta:
                    raw_events = await self._redis.lrange(self._events_key(debate_id), 0, -1)
                    meta = {
                        **raw_meta,
                        "max_turns": int(raw_meta.get("max_turns", 0)),
                        "total_turns": int(raw_meta.get("total_turns", 0)),
                        "total_tokens": int(raw_meta.get("total_tokens", 0)),
                        "agents": json.loads(raw_meta.get("agents", "[]")),
                    }
                    events = [json.loads(item) for item in raw_events]
                    return {"source": "redis", "meta": meta, "events": events}
            except Exception as e:
                print(f"[Storage] Redis read warning (get_debate): {e}")

        # Fallback: load permanent archive from disk
        path = self._json_path(debate_id)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            return {"source": "json", **doc}
        return None

    async def list_debates(self, limit: int = 25) -> list[dict[str, Any]]:
        """Returns metadata summaries of recent debates (newest first)."""
        summaries: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 1. Read hot debates from Redis first
        if self.redis_available and self._redis:
            try:
                ids = await self._redis.lrange(self._index_key(), 0, limit - 1)
                for did in ids:
                    raw_meta = await self._redis.hgetall(self._meta_key(did))
                    if raw_meta:
                        summaries.append({
                            "debate_id": did,
                            "topic": raw_meta.get("topic", ""),
                            "status": raw_meta.get("status", ""),
                            "total_turns": int(raw_meta.get("total_turns", 0)),
                            "total_tokens": int(raw_meta.get("total_tokens", 0)),
                            "end_reason": raw_meta.get("end_reason", ""),
                            "started_at": raw_meta.get("started_at", ""),
                            "agents": json.loads(raw_meta.get("agents", "[]")),
                            "source": "redis",
                        })
                        seen.add(did)
            except Exception as e:
                print(f"[Storage] Redis read warning (list_debates): {e}")

        # 2. Merge any older archived JSON files not currently in Redis
        json_files = sorted(self.debates_dir.glob("*.json"), reverse=True)
        for path in json_files:
            if len(summaries) >= limit:
                break
            did = path.stem
            if did in seen:
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                meta = doc.get("meta", {})
                summaries.append({
                    "debate_id": meta.get("debate_id", did),
                    "topic": meta.get("topic", ""),
                    "status": meta.get("status", "ended"),
                    "total_turns": int(meta.get("total_turns", 0)),
                    "total_tokens": int(meta.get("total_tokens", 0)),
                    "end_reason": meta.get("end_reason", ""),
                    "started_at": meta.get("started_at", ""),
                    "agents": meta.get("agents", []),
                    "source": "json",
                })
                seen.add(did)
            except Exception:
                continue

        return summaries