"""MCP サーバー — ask_agent ツールを提供する"""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

KOBITO_URL = os.environ.get("KOBITO_URL", "http://localhost:3956")
KOBITO_AGENT_ID = os.environ.get("KOBITO_AGENT_ID", "")

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
        "call_chain": [KOBITO_AGENT_ID] if KOBITO_AGENT_ID else [],
    }
    if session_id is not None:
        payload["session_id"] = session_id

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{KOBITO_URL}/api/internal/ask", json=payload)
        if not resp.is_success:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ValueError(f"HTTP {resp.status_code}: {detail}")
        return json.dumps(resp.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
