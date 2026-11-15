"""Serve IssueLens to Claude Code and Codex over stdio.

Installed, the issuelens-mcp command starts this server. From a checkout, run it from any
directory: uv run --directory /path/to/IssueLens --locked issuelens-mcp
"""

import asyncio
from dataclasses import replace
from importlib.metadata import version
import logging
import os
import sys
from typing import BinaryIO

from backend.app.config import Settings
from backend.app.mcp.analysis_tool import INSTRUCTIONS, IssueAnalysisTool
from backend.app.mcp.protocol import McpServer, Message, encode


# The installed distribution is the single source of the version reported to clients.
VERSION = version("issuelens")


def protect_stdout() -> BinaryIO:
    """Reserve the real stdout for protocol messages and send every other write to stderr.

    A stray print, from IssueLens or a library writing to file descriptor 1, would
    otherwise corrupt the JSON-RPC stream the client reads.
    """
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return protocol


def load_settings() -> tuple[Settings | None, str | None]:
    """Read the environment and settings_file(). A configuration error is reported by the tool, not by exiting."""
    try:
        # Analyses stay in this process's memory, and there is no sign-in, whatever .env sets for the web app.
        return replace(Settings.from_env(), database_url="", github_oauth=None), None
    except ValueError as exc:
        return None, str(exc)


async def serve(server: McpServer, stdin: BinaryIO) -> None:
    """Read one message per line until the client closes stdin, then stop in-flight calls."""
    loop = asyncio.get_running_loop()
    try:
        # A worker thread reads stdin, so the same code runs on Windows, where asyncio cannot read pipes.
        while line := await loop.run_in_executor(None, stdin.readline):
            if line.strip():
                await server.receive(line)
    finally:
        await server.close()


def main() -> None:
    protocol = protect_stdout()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="IssueLens MCP %(levelname)s: %(message)s")
    settings, setup_error = load_settings()
    analysis = IssueAnalysisTool(settings, setup_error)

    def send(message: Message) -> None:
        protocol.write(encode(message))
        protocol.flush()

    server = McpServer(
        name="issuelens", title="IssueLens", version=VERSION, instructions=INSTRUCTIONS,
        tools=[analysis.tool()], send=send,
    )

    async def run() -> None:
        try:
            await serve(server, sys.stdin.buffer)
        finally:
            await analysis.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
