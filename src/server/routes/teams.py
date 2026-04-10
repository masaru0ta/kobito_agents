"""チームエージェント — セッション管理・チャット API"""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.lmstudio_client import LMStudioClient, LMStudioTimeoutError
from server.routes.deps import get_config_manager
from server.team_chat import TeamChatProcessor
from server.team_session import TeamSession, TeamSessionManager, TeamSessionNotFoundError

router = APIRouter(prefix="/api/teams", tags=["teams"])


def _get_mgr(config: ConfigManager) -> TeamSessionManager:
    return TeamSessionManager(config._data_dir)


def _session_summary(s: TeamSession) -> dict:
    last_msg = ""
    if s.messages:
        last_msg = s.messages[-1].get("content", "")[:100]
    return {
        "session_id": s.session_id,
        "title": s.title,
        "updated_at": s.created_at,
        "last_message": last_msg,
        "message_count": len(s.messages),
        "initiated_by": None,
    }


@router.get("/{team_id}/sessions")
def list_team_sessions(team_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        config.get_agent(team_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"チーム '{team_id}' が見つかりません")
    mgr = _get_mgr(config)
    sessions = mgr.list_sessions(team_id)
    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return [_session_summary(s) for s in sessions]


@router.get("/{team_id}/sessions/{session_id}")
def get_team_session(
    team_id: str,
    session_id: str,
    config: ConfigManager = Depends(get_config_manager),
):
    mgr = _get_mgr(config)
    try:
        session = mgr.load_session(team_id, session_id)
    except TeamSessionNotFoundError:
        raise HTTPException(status_code=404, detail="セッションが見つかりません")
    return session.messages


class TitleUpdateRequest(BaseModel):
    title: str


@router.put("/{team_id}/sessions/{session_id}/title")
def update_team_session_title(
    team_id: str,
    session_id: str,
    body: TitleUpdateRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    mgr = _get_mgr(config)
    try:
        session = mgr.update_title(team_id, session_id, body.title)
    except TeamSessionNotFoundError:
        raise HTTPException(status_code=404, detail="セッションが見つかりません")
    return {"session_id": session.session_id, "title": session.title}


class TeamChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@router.post("/{team_id}/chat")
async def team_chat(
    team_id: str,
    body: TeamChatRequest,
    request: Request,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        team = config.get_agent(team_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"チーム '{team_id}' が見つかりません")

    if team.type != "team":
        raise HTTPException(status_code=400, detail="チームエージェントではありません")

    # メンバー情報を構築
    members = []
    for mid in team.members:
        try:
            m = config.get_agent(mid)
            members.append({"id": m.id, "name": m.name, "description": m.description})
        except AgentNotFoundError:
            pass

    mgr = _get_mgr(config)
    facilitator = LMStudioClient(config.get_setting("lmstudio_url"))
    max_turns = config.get_setting("team_max_turns")

    # LM Studio が未起動なら自動起動を試みる
    try:
        await asyncio.get_event_loop().run_in_executor(None, facilitator.ensure_running)
    except LMStudioTimeoutError:
        raise HTTPException(
            status_code=503,
            detail="LM Studio の起動がタイムアウトしました。手動で起動してから再試行してください。",
        )

    # セッション作成 or 読み込み
    if body.session_id:
        try:
            session = mgr.load_session(team_id, body.session_id)
        except TeamSessionNotFoundError:
            session = mgr.create_session(team_id, body.message[:50])
    else:
        session = mgr.create_session(team_id, body.message[:50])

    # ユーザーメッセージを保存
    session = mgr.append_message(team_id, session.session_id, {"role": "user", "content": body.message})
    current_session_id = session.session_id

    # 送信前の履歴（今回のユーザーメッセージを除く）
    prior_history = session.messages[:-1]

    base_url = str(request.base_url).rstrip("/")

    async def ask_agent_fn(agent_id: str, message: str, session_id=None):
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{base_url}/api/internal/ask",
                json={"agent_id": agent_id, "message": message, "call_chain": [team_id]},
            )
            if not resp.is_success:
                raise ValueError(f"HTTP {resp.status_code}: {resp.text}")
            return resp.json()

    processor = TeamChatProcessor(
        lmstudio_client=facilitator,
        ask_agent_fn=ask_agent_fn,
        max_turns=max_turns,
    )

    async def event_stream():
        yield f"data: {json.dumps({'type': 'session_id', 'data': current_session_id}, ensure_ascii=False)}\n\n"
        try:
            async for event in processor.process(
                members=members,
                title=session.title,
                history=prior_history,
                user_message=body.message,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "chunk":
                    mgr.append_message(team_id, current_session_id, {
                        "role": "agent",
                        "agent_id": event.get("agent_id", ""),
                        "agent_name": event.get("agent_name", ""),
                        "content": event["data"],
                    })
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
