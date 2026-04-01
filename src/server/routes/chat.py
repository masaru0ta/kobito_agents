"""チャット関連API"""

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.session_reader import SessionReader
from server.cli_bridge import CLIBridge, parse_stream_event, resolve_model
from server.routes.deps import get_config_manager, get_session_reader, get_cli_bridge

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["chat"])


class CLILaunchRequest(BaseModel):
    session_id: str | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@router.get("/sessions")
def list_sessions(
    agent_id: str,
    config: ConfigManager = Depends(get_config_manager),
    reader: SessionReader = Depends(get_session_reader),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    sessions = reader.list_sessions(agent.path)
    return [s.model_dump() if hasattr(s, 'model_dump') else s.dict() for s in sessions]


@router.get("/sessions/{session_id}")
def get_session(
    agent_id: str,
    session_id: str,
    config: ConfigManager = Depends(get_config_manager),
    reader: SessionReader = Depends(get_session_reader),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    messages = reader.read_session(agent.path, session_id)
    return [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in messages]


@router.post("/chat")
async def send_chat(
    agent_id: str,
    body: ChatRequest,
    config: ConfigManager = Depends(get_config_manager),
    bridge: CLIBridge = Depends(get_cli_bridge),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")

    model = resolve_model(agent.cli, agent.model_tier)

    async def event_stream():
        async for raw_event in bridge.run_stream(
            project_path=agent.path,
            prompt=body.message,
            model=model,
            session_id=body.session_id,
            system_prompt=agent.system_prompt if not body.session_id else None,
        ):
            ev = parse_stream_event(raw_event)
            if ev.event_type == "assistant" and ev.text:
                yield f"data: {json.dumps({'type': 'chunk', 'data': ev.text}, ensure_ascii=False)}\n\n"
                for tu in ev.tool_uses:
                    desc = tu.get("name", "")
                    inp = tu.get("input", {})
                    if inp.get("file_path"):
                        desc += f": {inp['file_path'].split('/')[-1].split(chr(92))[-1]}"
                    elif inp.get("command"):
                        desc += f": {inp['command'][:60]}"
                    yield f"data: {json.dumps({'type': 'tool_use', 'data': desc}, ensure_ascii=False)}\n\n"
            elif ev.event_type == "result":
                yield f"data: {json.dumps({'type': 'session_id', 'data': ev.session_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/hide")
def hide_session(
    agent_id: str,
    session_id: str,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    import json
    from pathlib import Path
    meta_dir = Path(agent.path) / ".kobito" / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / f"{session_id}.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["hidden"] = True
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok"}


@router.post("/cli")
def launch_cli(
    agent_id: str,
    body: CLILaunchRequest,
    config: ConfigManager = Depends(get_config_manager),
    bridge: CLIBridge = Depends(get_cli_bridge),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    bridge.launch_cli(agent.path, body.session_id)
    return {"status": "ok"}
