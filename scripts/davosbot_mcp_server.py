#!/usr/bin/env python3
"""Minimal stdio MCP server exposing safe DavosBot operator tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, BinaryIO


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import codex_operator  # noqa: E402


SERVER_INFO = {"name": "davosbot-operator", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else ""
        return _response(
            request_id,
            {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": codex_operator.tool_specs_for_mcp()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid tools/call params")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(request_id, -32602, "tools/call requires string name and object arguments")
        result = codex_operator.run_tool(name, arguments)
        return _response(
            request_id,
            {
                "content": [{"type": "text", "text": result.text}],
                "isError": not result.ok,
            },
        )
    return _error(request_id, -32601, f"Unknown method: {method}")


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    payload = stream.read(length)
    return json.loads(payload.decode("utf-8"))


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def main() -> int:
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while True:
        message = read_message(reader)
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            write_message(writer, response)


if __name__ == "__main__":
    raise SystemExit(main())
