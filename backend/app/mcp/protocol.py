"""The MCP adapter: JSON-RPC 2.0 messages, the initialize handshake, and the tools feature.

Only what IssueLens needs is implemented: initialize, ping, tools/list, tools/call,
cancellation and progress notifications. Both clients tested in M5 negotiate one of SUPPORTED_PROTOCOL_VERSIONS, which
share the same messages for these methods. The stateless 2026-07-28 revision starts with
server/discover; answering it with "method not found" makes a client fall back to initialize.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
from typing import Any


logger = logging.getLogger(__name__)
# Newest first; a client asking for another version is offered the newest.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RequestId = str | int
Message = dict[str, Any]
# Report one human-readable progress message for the current call. It does nothing when
# the client did not ask for progress or the call has finished.
Progress = Callable[[str], None]


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any], Progress], Awaitable[ToolResult]]
    annotations: dict[str, Any] = field(default_factory=dict)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name, "title": self.title, "description": self.description,
            "inputSchema": self.input_schema, "annotations": self.annotations,
        }


def error_response(request_id: RequestId | None, code: int, message: str) -> Message:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def encode(message: Message) -> bytes:
    """Frame one message for stdio. JSON escapes newlines, so the frame is a single line."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class McpServer:
    def __init__(
        self, *, name: str, title: str, version: str, instructions: str, tools: list[Tool],
        send: Callable[[Message], None],
    ) -> None:
        self.server_info = {"name": name, "title": title, "version": version}
        self.instructions = instructions
        self.tools = {tool.name: tool for tool in tools}
        self.send = send
        self.calls: dict[RequestId, asyncio.Task[None]] = {}

    async def receive(self, line: bytes) -> None:
        """Handle one framed message. Tool calls run as tasks so the reader can take cancellations."""
        try:
            message = json.loads(line)
        except ValueError:
            self.send(error_response(None, PARSE_ERROR, "Parse error: each line must be one JSON-RPC message."))
            return
        if not isinstance(message, dict):
            # JSON-RPC batches were removed from MCP in 2025-06-18.
            self.send(error_response(None, INVALID_REQUEST, "Invalid request: send one JSON-RPC object per line."))
            return
        request_id = message.get("id")
        valid_id = isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
        if message.get("jsonrpc") != "2.0" or ("id" in message and not valid_id):
            self.send(error_response(request_id if valid_id else None, INVALID_REQUEST, "Invalid request."))
            return
        if "method" not in message:
            return  # A response to a server request; this server sends none.
        method, params = message["method"], message.get("params", {})
        if not isinstance(method, str):
            if valid_id:
                self.send(error_response(request_id, INVALID_REQUEST, "Invalid request: method must be a string."))
            return
        if "id" not in message:
            self._notify(method, params)
            return
        assert isinstance(request_id, (str, int))
        if not isinstance(params, dict):
            self.send(error_response(request_id, INVALID_PARAMS, "Invalid params: params must be an object."))
        elif method == "tools/call":
            if request_id in self.calls:
                self.send(error_response(request_id, INVALID_REQUEST, "Invalid request: this id is already in use."))
                return
            task = asyncio.create_task(self._call_tool(request_id, params))
            self.calls[request_id] = task
            task.add_done_callback(lambda _: self.calls.pop(request_id, None))
        else:
            self.send(self._answer(request_id, method, params))

    def _answer(self, request_id: RequestId, method: str, params: dict[str, Any]) -> Message:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            result: dict[str, Any] = {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.server_info, "instructions": self.instructions,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [tool.definition() for tool in self.tools.values()]}
        else:
            return error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _notify(self, method: str, params: object) -> None:
        if method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            # Unknown or finished requests are ignored, as the specification allows.
            task = self.calls.get(request_id) if isinstance(request_id, (str, int)) else None
            if task is not None:
                task.cancel()

    def _progress(self, params: dict[str, Any]) -> tuple[Progress, Callable[[], None]]:
        """Return a progress reporter for one call, and a function that stops it."""
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        # Codex uses 0 as a token, so test the type rather than truthiness.
        active = isinstance(token, (str, int)) and not isinstance(token, bool)
        count = 0

        def report(message: str) -> None:
            nonlocal count
            if not active:
                return
            # This server counts the notifications, so progress always increases as required.
            count += 1
            self.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                "progressToken": token, "progress": count, "message": message,
            }})

        def stop() -> None:
            nonlocal active
            active = False  # Notifications must stop when the call completes or is cancelled.

        return report, stop

    async def _call_tool(self, request_id: RequestId, params: dict[str, Any]) -> None:
        name, arguments = params.get("name"), params.get("arguments", {})
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            self.send(error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name}"))
            return
        progress, stop_progress = self._progress(params)
        try:
            if not isinstance(arguments, dict):
                result = ToolResult("Tool arguments must be a JSON object.", is_error=True)
            else:
                result = await tool.run(arguments, progress)
        except asyncio.CancelledError:
            raise  # A cancelled request gets no response.
        except Exception as exc:
            # Exception text can repeat upstream responses or credentials; log the type only.
            logger.error("Tool %s failed with %s", tool.name, type(exc).__name__)
            result = ToolResult("IssueLens failed unexpectedly. Check the MCP server log.", is_error=True)
        finally:
            stop_progress()
        self.send({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": result.text}], "isError": result.is_error,
        }})

    async def close(self) -> None:
        """Stop in-flight tool calls, for example when the client closes stdin."""
        tasks = list(self.calls.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
