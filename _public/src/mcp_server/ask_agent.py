"""MCP サーバー — ask_agent ツールを提供する"""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

KOBITO_URL = os.environ.get("KOBITO_URL", "http://localhost:3956")

mcp = FastMCP("kobito-ask-agent")


@mcp.tool()
async def ask_agent(
    agent_id: str,
    message: str,
    session_id: str | None = None,
) -> str:
    """他のエージェントにメッセージを送り、回答を得る"""
    payload: dict = {
        "agent_id": agent_id,
        "message": message,
    }
    if session_id is not None:
        payload["session_id"] = session_id

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{KOBITO_URL}/api/internal/ask", json=payload)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
