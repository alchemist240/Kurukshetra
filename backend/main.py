import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.agents.providers.groq_agent import GroqAgent
from backend.agents.providers.gemini_agent import GeminiAgent
from backend.agents.providers.openai_agent import OpenAIAgent
from backend.agents.providers.deepseek_agent import DeepSeekAgent
from backend.core.events import EventBus
from backend.core.registry import AgentRegistry
from backend.core.orchestrator import ArenaOrchestrator
from backend.storage.store import DebateStore

# 1. Boot shared EventBus, AgentRegistry, DebateStore, and Orchestrator
event_bus = EventBus()
registry = AgentRegistry()
store = DebateStore()

for agent in [GroqAgent(), GeminiAgent(), OpenAIAgent(), DeepSeekAgent()]:
    registry.register(agent)

orchestrator = ArenaOrchestrator(registry, event_bus, store=store)

# Track running background tasks so we never overlap two debates or two wake-ups
active_debate_task: asyncio.Task | None = None
active_wakeup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Connects to Redis on startup and cleanly closes the pool on shutdown."""
    await store.connect()
    yield
    await store.close()


# 2. Initialize FastAPI with lifespan and mount frontend static assets
app = FastAPI(title="Kurukshetra — Multi-Agent Arena", lifespan=lifespan)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def trigger_wakeup() -> None:
    """Starts a zero-prompt wake-up if no wake-up or debate is already running."""
    global active_wakeup_task
    if orchestrator.is_running:
        return
    if active_wakeup_task and not active_wakeup_task.done():
        return
    active_wakeup_task = asyncio.create_task(orchestrator.wake_up_arena())


# --- HTTP Routes (UI + Debate History API) ---

@app.get("/")
async def serve_arena_ui():
    """Serves the main HTML page when you open http://127.0.0.1:8000."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/debates")
async def list_saved_debates(limit: int = 25):
    """Returns recent debate summaries (Redis + JSON archive) and storage mode."""
    debates = await store.list_debates(limit=limit)
    return {
        "redis_available": store.redis_available,
        "debates": debates,
    }


@app.get("/api/debates/{debate_id}")
async def get_saved_debate(debate_id: str):
    """Loads one full saved debate transcript for replay."""
    debate = await store.get_debate(debate_id)
    if not debate:
        raise HTTPException(status_code=404, detail="Debate not found")
    return debate


# --- Live WebSocket Route ---

@app.websocket("/ws/arena")
async def arena_websocket(websocket: WebSocket):
    """Live bidirectional bridge between the browser UI and ArenaOrchestrator."""
    global active_debate_task
    await websocket.accept()

    queue = event_bus.subscribe()

    async def sender_loop():
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    sender_task = asyncio.create_task(sender_loop())

    try:
        # Send initial warrior roster immediately
        await websocket.send_json({
            "type": "roster_update",
            "data": {"agents": registry.get_peer_roster()},
        })

        # Self-invoke all 4 agents to greet you automatically on page load
        trigger_wakeup()

        while True:
            cmd = await websocket.receive_json()
            action = cmd.get("action")

            if action == "start_debate":
                topic = str(cmd.get("topic", "")).strip()
                agent_ids = cmd.get("agent_ids") or []
                max_turns = int(cmd.get("max_turns", 12))

                if not topic:
                    await event_bus.publish("system_notice", {"message": "Enter a topic before starting."})
                elif active_debate_task and not active_debate_task.done():
                    await event_bus.publish("system_notice", {"message": "A debate is already running."})
                else:
                    active_debate_task = asyncio.create_task(
                        orchestrator.run_debate(
                            topic=topic,
                            agent_ids=agent_ids,
                            max_turns=max_turns,
                        )
                    )

            elif action == "pause":
                orchestrator.pause()

            elif action == "resume":
                orchestrator.resume()

            elif action == "stop":
                orchestrator.stop()

            elif action == "nudge":
                orchestrator.add_user_nudge(str(cmd.get("message", "")))

            elif action == "wake_up":
                trigger_wakeup()

    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        event_bus.unsubscribe(queue)