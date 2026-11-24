import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "mcp_servers.json"
MAX_TOOL_OUTPUT_CHARS = 3200
TOOL_TIMEOUT_SECONDS = 20.0


class MCPManager:
    """
    Plug-and-play MCP Client Gateway for Kurukshetra.
    Reads mcp_servers.json, launches local stdio MCP servers, discovers all tools
    dynamically, and routes agent tool calls through one auditable choke point.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._exit_stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._tool_to_server: dict[str, str] = {}
        self._tools_catalog: list[dict[str, Any]] = []

    async def connect_all(self) -> list[dict[str, Any]]:
        """Starts all enabled MCP servers from mcp_servers.json and discovers their tools."""
        if not self.config_path.exists():
            print(f"[MCP] Config not found at {self.config_path}. Running without MCP tools.")
            return []

        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        servers = config.get("servers", {})

        self._exit_stack = AsyncExitStack()
        self._sessions.clear()
        self._tool_to_server.clear()
        self._tools_catalog.clear()

        for server_name, cfg in servers.items():
            if not cfg.get("enabled", True):
                continue

            try:
                raw_cmd = str(cfg.get("command", "python"))
                # Always use the active venv Python when config says "python"
                command = sys.executable if raw_cmd in ("python", "python3") else raw_cmd

                raw_args = cfg.get("args", [])
                resolved_args = [
                    str(PROJECT_ROOT / arg) if (PROJECT_ROOT / arg).exists() else str(arg)
                    for arg in raw_args
                ]

                # Pass current environment (including .env keys like TAVILY_API_KEY) to subprocess
                env = os.environ.copy()
                env.update({k: str(v) for k, v in cfg.get("env", {}).items()})

                params = StdioServerParameters(command=command, args=resolved_args, env=env)
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()

                # Dynamically discover every tool exposed by this MCP server
                tools_resp = await session.list_tools()
                self._sessions[server_name] = session

                for tool in tools_resp.tools:
                    self._tool_to_server[tool.name] = server_name
                    schema = (
                        getattr(tool, "input_schema", None)
                        or getattr(tool, "inputSchema", None)
                        or {}
                    )
                    self._tools_catalog.append({
                        "name": tool.name,
                        "server": server_name,
                        "description": (tool.description or "").strip(),
                        "input_schema": schema,
                    })

                tool_names = [t.name for t in tools_resp.tools]
                print(f"[MCP] Connected server '{server_name}' -> tools: {tool_names}")

            except Exception as e:
                print(f"[MCP] Failed to start server '{server_name}': {type(e).__name__}: {e}")

        return self.list_tools()

    async def close_all(self) -> None:
        """Cleanly shuts down every running MCP server subprocess."""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass
            self._exit_stack = None
        self._sessions.clear()
        self._tool_to_server.clear()
        self._tools_catalog.clear()

    def list_tools(self) -> list[dict[str, Any]]:
        """Returns discovered tools metadata (used by UI and Phase 2 Guardrails)."""
        return list(self._tools_catalog)

    def has_tools(self) -> bool:
        return len(self._tools_catalog) > 0

    def format_tools_for_prompt(self) -> str:
        """Formats discovered MCP tools into a compact menu injected into agent prompts."""
        if not self._tools_catalog:
            return "- None available."

        lines = []
        for tool in self._tools_catalog:
            props = tool.get("input_schema", {}).get("properties", {})
            required = set(tool.get("input_schema", {}).get("required", []))
            arg_parts = []
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "any")
                req_tag = "" if pname in required else ", optional"
                arg_parts.append(f"{pname}: {ptype}{req_tag}")
            sig = f"{tool['name']}({', '.join(arg_parts)})"
            desc = " ".join(tool["description"].split())
            lines.append(f"- {sig} [server: {tool['server']}]: {desc}")
        return "\n".join(lines)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Executes a tool on its owning MCP server with timeout and output-size protection."""
        args = arguments or {}
        server_name = self._tool_to_server.get(tool_name)
        if not server_name or server_name not in self._sessions:
            return {
                "ok": False,
                "server": "unknown",
                "tool_name": tool_name,
                "arguments": args,
                "output": f"Tool '{tool_name}' is not registered on any connected MCP server.",
                "latency_ms": 0,
            }

        session = self._sessions[server_name]
        start = time.perf_counter()

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=args),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)

            # Extract text chunks from the MCP CallToolResult object
            text_chunks = [
                block.text for block in (result.content or []) if hasattr(block, "text")
            ]
            output = "\n".join(text_chunks).strip() or "(Tool returned empty output.)"
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n... [truncated]"

            # MCP SDK v2 uses snake_case (is_error); fall back to v1 (isError)
            is_err = bool(getattr(result, "is_error", getattr(result, "isError", False)))

            return {
                "ok": not is_err,
                "server": server_name,
                "tool_name": tool_name,
                "arguments": args,
                "output": output,
                "latency_ms": latency_ms,
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "server": server_name,
                "tool_name": tool_name,
                "arguments": args,
                "output": f"Tool '{tool_name}' timed out after {TOOL_TIMEOUT_SECONDS}s.",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        except Exception as e:
            return {
                "ok": False,
                "server": server_name,
                "tool_name": tool_name,
                "arguments": args,
                "output": f"Tool execution error: {type(e).__name__}: {e}",
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }