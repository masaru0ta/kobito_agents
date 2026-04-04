"""エージェント関連API"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager, DuplicatePathError, SystemAgentProtectedError
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentCreateRequest(BaseModel):
    name: str
    path: str
    description: str = ""
    cli: str = "claude"
    model_tier: str = "deep"


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    model_tier: str | None = None


class SystemPromptRequest(BaseModel):
    content: str


@router.get("")
def list_agents(config: ConfigManager = Depends(get_config_manager)):
    agents = config.list_agents()
    return [
        {
            "id": a.id, "name": a.name, "path": a.path,
            "description": a.description, "cli": a.cli, "model_tier": a.model_tier,
        }
        for a in agents
    ]


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
    return {
        "id": a.id, "name": a.name, "path": a.path,
        "description": a.description, "cli": a.cli, "model_tier": a.model_tier,
    }


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        config.delete_agent(agent_id)
    except SystemAgentProtectedError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@router.get("/{agent_id}")
def get_agent(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        a = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return {
        "id": a.id, "name": a.name, "path": a.path,
        "description": a.description, "cli": a.cli, "model_tier": a.model_tier,
    }


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
    return {
        "id": a.id, "name": a.name, "path": a.path,
        "description": a.description, "cli": a.cli, "model_tier": a.model_tier,
    }


@router.get("/{agent_id}/system-prompt")
def get_system_prompt(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        content = config.get_system_prompt(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return {"content": content}


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
