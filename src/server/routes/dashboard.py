"""ダッシュボードAPI"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents", tags=["dashboard"])

_DASHBOARD_PATH = ".kobito/dashboard.md"


class DashboardContent(BaseModel):
    content: str


@router.get("/{agent_id}/dashboard")
def get_dashboard(agent_id: str, config: ConfigManager = Depends(get_config_manager)):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail="エージェントが見つかりません")

    path = Path(agent.path) / _DASHBOARD_PATH
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"content": content}


@router.put("/{agent_id}/dashboard")
def put_dashboard(
    agent_id: str,
    body: DashboardContent,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail="エージェントが見つかりません")

    path = Path(agent.path) / _DASHBOARD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"status": "ok"}
