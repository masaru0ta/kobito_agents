"""エージェント関連API"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.config import AgentInfo, AgentNotFoundError, ConfigManager, DuplicatePathError, SystemAgentProtectedError
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents", tags=["agents"])

_ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _agent_dict(a: AgentInfo) -> dict:
    return {
        "id": a.id, "name": a.name, "path": a.path,
        "description": a.description, "cli": a.cli, "model_tier": a.model_tier,
        "thumbnail_url": a.thumbnail_url,
    }


class AgentCreateRequest(BaseModel):
    name: str
    path: str
    description: str = ""
    cli: str = "claude"
    model_tier: str = "quick"


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    model_tier: str | None = None


class SystemPromptRequest(BaseModel):
    content: str


@router.get("")
def list_agents(config: ConfigManager = Depends(get_config_manager)):
    return [_agent_dict(a) for a in config.list_agents()]


@router.post("")
def create_agent(body: AgentCreateRequest, config: ConfigManager = Depends(get_config_manager)):
    try:
        a = config.add_agent(
            name=body.name, path=body.path, description=body.description,
            cli=body.cli, model_tier=body.model_tier,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicatePathError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _agent_dict(a)


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        config.delete_agent(agent_id)
    except SystemAgentProtectedError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@router.get("/{agent_id}/thumbnail")
def get_thumbnail(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    p = config.get_thumbnail_path(agent_id)
    if not p:
        raise HTTPException(status_code=404, detail="サムネイルが設定されていません")
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/{agent_id}/thumbnail")
async def upload_thumbnail(
    agent_id: str,
    file: UploadFile = File(...),
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    ext = _ALLOWED_CONTENT_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(status_code=400, detail="png / jpg / gif / webp のみ対応しています")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="ファイルサイズは5MB以下にしてください")
    config.save_thumbnail(agent_id, data, ext)
    return {"thumbnail_url": f"/api/agents/{agent_id}/thumbnail"}


@router.delete("/{agent_id}/thumbnail")
def delete_thumbnail(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    config.delete_thumbnail(agent_id)
    return {"status": "ok"}


@router.get("/{agent_id}")
def get_agent(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        a = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return _agent_dict(a)


@router.put("/{agent_id}")
def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        a = config.update_agent(agent_id, **body.model_dump(exclude_none=True))
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return _agent_dict(a)


@router.get("/{agent_id}/system-prompt")
def get_system_prompt(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        content = config.get_system_prompt(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    shared_path = Path(__file__).resolve().parents[2] / "assets" / "prompts" / "shared_instructions.md"
    shared = shared_path.read_text(encoding="utf-8") if shared_path.exists() else None
    return {"content": content, "shared_instructions": shared}


@router.put("/{agent_id}/system-prompt")
def update_system_prompt(
    agent_id: str,
    body: SystemPromptRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        config.update_system_prompt(agent_id, body.content)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return {"status": "ok"}
