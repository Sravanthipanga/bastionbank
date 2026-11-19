"""MCP client wrapper — the ONLY way agents reach bank data.

Transport: real MCP protocol over stdio subprocesses (one subprocess per
server, launched with the project venv's interpreter). A dedicated thread
runs the asyncio event loop that owns the two ClientSessions; the rest of
the app uses the small synchronous API below.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import Future

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVERS = {
    "accounts": "bastionbank.mcp_servers.accounts_server",
    "payments": "bastionbank.mcp_servers.payments_server",
}


class MCPClient:
    def __init__(self, db_path: str, project_root: str):
        self.db_path = db_path
        self.project_root = project_root
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._ready = threading.Event()
        self._startup_error: Exception | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-client")
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise TimeoutError("MCP client failed to start within 30s")
        if self._startup_error:
            raise RuntimeError(f"MCP client startup failed: {self._startup_error}")

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_all())
        except Exception as exc:  # surfaced via start()
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _connect_all(self) -> None:
        env = dict(os.environ)
        env["BASTIONBANK_DB"] = self.db_path
        env["PYTHONPATH"] = self.project_root
        for name, module in SERVERS.items():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", module],
                env=env,
                cwd=self.project_root,
            )
            stack = _SessionStack()
            read, write = await stack.enter(stdio_client(params))
            session = await stack.enter(ClientSession(read, write))
            await session.initialize()
            self._sessions[name] = session
            setattr(self, f"_stack_{name}", stack)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # -- synchronous API -----------------------------------------------------
    def _submit(self, coro) -> Future:
        if not self._loop:
            raise RuntimeError("MCP client not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def list_tools(self, server: str) -> list[str]:
        async def _list():
            result = await self._sessions[server].list_tools()
            return [t.name for t in result.tools]
        return self._submit(_list()).result(timeout=30)

    def call_tool(self, server: str, tool: str, args: dict) -> dict:
        async def _call():
            result = await self._sessions[server].call_tool(tool, args)
            if result.is_error:
                text = result.content[0].text if result.content else "tool error"
                return {"error": text}
            if getattr(result, "structured_content", None):
                return dict(result.structured_content)
            # Fall back to parsing the text content block as JSON.
            import json
            text = result.content[0].text if result.content else "{}"
            try:
                return json.loads(text)
            except Exception:
                return {"text": text}
        return self._submit(_call()).result(timeout=30)


class _SessionStack:
    """Minimal async context stack (avoids needing anyio ExitStack plumbing)."""

    def __init__(self):
        self._contexts = []

    async def enter(self, ctx):
        value = await ctx.__aenter__()
        self._contexts.append(ctx)
        return value
