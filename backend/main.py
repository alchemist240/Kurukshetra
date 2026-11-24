import asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.agents.providers.groq_agent import GroqAgent
from backend.agents.providers.gemini_agent import GeminiAgent
from backend.agents.providers.openai_agent import OpenAIAgent
from backend.agents.providers.deepseek_agent import DeepSeekAgent
from backend.core.events import EventBus
from backend.core.registry import AgentRegistry
from backend.core.orchestrator import ArenaOrchestrator

# 1. Initialize FastAPI and locate the frontend folder
app = FastAPI(title="Kurukshetra — Multi-Agent Arena")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# 2. Boot the shared EventBus, AgentRegistry, and Orchestrator once on startup
event_bus = EventBus()
registry = AgentRegistry()

for agent in [GroqAgent(), GeminiAgent(), OpenAIAgent(), DeepSeekAgent()]:
    registry.register(agent)

orchestrator = ArenaOrchestrator(registry, event_bus)

# Track running background tasks so we never overlap two debates or two wake-ups
active_debate_task: asyncio.Task | None = None
active_wakeup_task: asyncio.Task | None = None


def trigger_wakeup() -> None:
    """Starts a zero-prompt wake-up if no wake-up or debate is already running."""
    global active_wakeup_task
    if orchestrator.is_running:
        return
    if active_wakeup_task and not active_wakeup_task.done():
        return
    active_wakeup_task = asyncio.create_task(orchestrator.wake_up_arena())


@app.get("/")
async def serve_arena_ui():
    """Serves the main HTML page when you open http://127.0.0.1:8000."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.websocket("/ws/arena")
async def arena_websocket(websocket: WebSocket):
    """Live bidirectional bridge between the browser UI and ArenaOrchestrator."""
    global active_debate_task
    await websocket.accept()

    # Subscribe this browser tab to all live Arena events
    queue = event_bus.subscribe()

    async def sender_loop():
        """Reads events from EventBus and pushes them to the browser as JSON."""
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    sender_task = asyncio.create_task(sender_loop())

    try:
        # Immediately send the current 4-agent roster so cards appear right away
        await websocket.send_json({
            "type": "roster_update",
            "data": {"agents": registry.get_peer_roster()},
        })

        # Self-invoke all 4 agents to greet you automatically on page load
        trigger_wakeup()

        # Listen continuously for commands clicked in the UI
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
                    # Run in the background so Pause/Stop/Nudge commands still arrive live
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
        # Normal clean exit when you close or refresh the browser tab
        pass
    finally:
        sender_task.cancel()
        event_bus.unsubscribe(queue)