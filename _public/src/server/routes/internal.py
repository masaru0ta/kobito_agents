"""内部API — エージェント間通信用（UIからは呼ばれない）"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.cli_bridge import CLIBridge, parse_stream_event, resolve_model
from server.routes.deps import get_config_manager, get_cli_bridge
from server.routes.chat import _update_session_meta

router = APIRouter(prefix="/api/internal", tags=["internal"])

ASK_TIMEOUT = 300  # 5分
MAX_CALL_CHAIN = 5  # A→B→C→D→E まで


class AskRequest(BaseModel):
    agent_id: str
    message: str
    session_id: str | None = None
    call_chain: list[str] | None = None


async def _consume_stream(stream) -> tuple[str, str]:
    """ストリームを消費してテキストとsession_idを返す"""
    accumulated_text = ""
    session_id = ""
    async for raw_event in stream:
        etype = raw_event.get("type")
        if etype == "_ping":
            continue
        ev = parse_stream_event(raw_event)
        if ev.event_type == "assistant" and ev.text:
            accumulated_text += ev.text
        elif ev.event_type == "result":
            session_id = ev.session_id
    return accumulated_text, session_id


@router.post("/ask")
async def ask_agent(
    body: AskRequest,
    config: ConfigManager = Depends(get_config_manager),
    bridge: CLIBridge = Depends(get_cli_bridge),
):
    # ループ検出: call_chain に送信先が含まれていたら拒否
    chain = body.call_chain or []
    if body.agent_id in chain:
        raise HTTPException(status_code=400, detail="ループが検出されました")
    if len(chain) >= MAX_CALL_CHAIN:
        raise HTTPException(status_code=400, detail="呼び出しチェーンが最大長を超えました")

    # エージェント存在確認
    try:
        agent = config.get_agent(body.agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"エージェント '{body.agent_id}' が見つかりません",
        )

    model = resolve_model(agent.cli, agent.model_tier)

    # 発信者情報をメッセージに注入
    caller_id = chain[0] if chain else "system"
    try:
        caller = config.get_agent(caller_id)
        caller_name = caller.name
    except AgentNotFoundError:
        caller_name = caller_id
    prompt = f"[{caller_name}からのメッセージ]\n{body.message}"

    # CLIBridge でストリームを内部消費し、テキストを蓄積
    try:
        stream = bridge.run_stream(
            project_path=agent.path,
            prompt=prompt,
            model=model,
            session_id=body.session_id,
        )
        accumulated_text, result_session_id = await asyncio.wait_for(
            _consume_stream(stream), timeout=ASK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="タイムアウト")

    # 新規セッションの場合、呼び出し元情報をメタに記録
    if not body.session_id and result_session_id:
        caller = chain[0] if chain else "system"
        _update_session_meta(agent.path, result_session_id, {
            "initiated_by": caller,
        })

    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "session_id": result_session_id,
        "response": accumulated_text,
    }
