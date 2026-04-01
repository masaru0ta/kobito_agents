"""チャット関連API"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.session_reader import SessionReader
from server.cli_bridge import CLIBridge
from server.routes.deps import get_config_manager, get_session_reader, get_cli_bridge

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["chat"])


class CLILaunchRequest(BaseModel):
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
