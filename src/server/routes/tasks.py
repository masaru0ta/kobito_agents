"""タスク管理API"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import ConfigManager, AgentNotFoundError
from server.task_manager import TaskManager
from server.routes.deps import get_config_manager

router = APIRouter()


def _get_task_manager(agent_id: str, cfg: ConfigManager) -> TaskManager:
    try:
        agent = cfg.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return TaskManager(Path(agent.path))


def _write_session_meta_task(agent_path: str, session_id: str, task_id: str, task_title: str) -> None:
    """セッションメタに linked_task を書き込む（セッション→タスクの逆リンク）"""
    p = Path(agent_path) / ".kobito" / "meta" / f"{session_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    if p.exists():
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta["linked_task"] = task_id
    meta["linked_task_title"] = task_title
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@router.get("/api/agents/{agent_id}/tasks")
async def list_tasks(agent_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    tasks = tm.list_tasks()
    order = tm.get_order()
    return {"tasks": [t.model_dump() for t in tasks], "order": order}


@router.get("/api/agents/{agent_id}/tasks/{task_id}")
async def get_task(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        return tm.get_task(task_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


@router.post("/api/agents/{agent_id}/tasks/{task_id}/approve")
async def approve_task(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        return tm.approve(task_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")



@router.post("/api/agents/{agent_id}/tasks/{task_id}/force-done")
async def force_done(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        return tm.force_done(task_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


@router.delete("/api/agents/{agent_id}/tasks/{task_id}")
async def delete_task(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    tm.delete(task_id)
    return {"ok": True}


class OrderBody(BaseModel):
    order: list[str]


@router.put("/api/agents/{agent_id}/tasks/order")
async def update_order(agent_id: str, body: OrderBody, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    return {"order": tm.update_order(body.order)}


class SessionBody(BaseModel):
    session_id: str


@router.post("/api/agents/{agent_id}/tasks/{task_id}/sessions")
async def add_session(agent_id: str, task_id: str, body: SessionBody, cfg: ConfigManager = Depends(get_config_manager)):
    try:
        agent = cfg.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    tm = TaskManager(Path(agent.path))
    try:
        task = tm.add_session(task_id, body.session_id)
        _write_session_meta_task(agent.path, body.session_id, task_id, task.title)
        return task.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


@router.put("/api/agents/{agent_id}/tasks/{task_id}/talk-session")
async def set_talk_session(agent_id: str, task_id: str, body: SessionBody, cfg: ConfigManager = Depends(get_config_manager)):
    try:
        agent = cfg.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    tm = TaskManager(Path(agent.path))
    try:
        task = tm.set_talk_session(task_id, body.session_id)
        _write_session_meta_task(agent.path, body.session_id, task_id, task.title)
        return task.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


class TaskBodyUpdate(BaseModel):
    body: str


@router.put("/api/agents/{agent_id}/tasks/{task_id}")
async def update_task_body(agent_id: str, task_id: str, update: TaskBodyUpdate, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        return tm.update_body(task_id, update.body).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


# ----------------------------------------------------------------
# 定期タスク設定 API（Phase10）
# ----------------------------------------------------------------


class RecurringBody(BaseModel):
    reset_interval: str
    reset_time: str | None = None
    reset_weekday: str | None = None
    reset_monthday: int | None = None
    repeat_enabled: bool | None = None


@router.get("/api/agents/{agent_id}/tasks/{task_id}/recurring")
async def get_recurring(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        return tm.get_recurring(task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


@router.put("/api/agents/{agent_id}/tasks/{task_id}/recurring")
async def set_recurring(agent_id: str, task_id: str, body: RecurringBody, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        tm.set_recurring(
            task_id,
            reset_interval=body.reset_interval,
            reset_time=body.reset_time,
            reset_weekday=body.reset_weekday,
            reset_monthday=body.reset_monthday,
            repeat_enabled=body.repeat_enabled,
        )
        return tm.get_recurring(task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")


@router.delete("/api/agents/{agent_id}/tasks/{task_id}/recurring")
async def clear_recurring(agent_id: str, task_id: str, cfg: ConfigManager = Depends(get_config_manager)):
    tm = _get_task_manager(agent_id, cfg)
    try:
        tm.clear_recurring(task_id)
        return tm.get_recurring(task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"タスク '{task_id}' が見つかりません")
