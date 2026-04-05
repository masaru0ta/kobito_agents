"""ファイルリンクAPI — ファイルとチャットセッションの紐づけを管理する"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents/{agent_id}/file-links", tags=["file-links"])


def _link_path(agent_path: str, file_path: str) -> Path:
    """ファイルパスに対応するリンクメタデータファイルのパスを返す"""
    safe = file_path.replace("/", "__").replace("\\", "__").replace(":", "__")
    return Path(agent_path) / ".kobito" / "file-links" / f"{safe}.json"


def _meta_path(agent_path: str, session_id: str) -> Path:
    return Path(agent_path) / ".kobito" / "meta" / f"{session_id}.json"


def _read_link(agent_path: str, file_path: str) -> dict | None:
    p = _link_path(agent_path, file_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _write_link(agent_path: str, file_path: str, data: dict) -> None:
    p = _link_path(agent_path, file_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_session_meta(agent_path: str, session_id: str, file_path: str, title: str) -> None:
    p = _meta_path(agent_path, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    meta: dict = {}
    if p.exists():
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta["linked_file"] = file_path
    if not meta.get("title"):
        meta["title"] = title
    if not meta.get("created"):
        meta["created"] = now
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@router.get("")
def get_file_link(
    agent_id: str,
    path: str,
    config: ConfigManager = Depends(get_config_manager),
):
    """ファイルに紐づくセッションIDを返す。なければ null。"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")

    link = _read_link(agent.path, path)
    if not link:
        return {"session_id": None}

    # セッションメタが存在するか確認
    sid = link.get("session_id")
    if sid and _meta_path(agent.path, sid).exists():
        return {"session_id": sid}
    return {"session_id": None}


class CreateLinkRequest(BaseModel):
    file_path: str
    title: str = ""


@router.post("")
def create_file_link(
    agent_id: str,
    body: CreateLinkRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    """ファイルに紐づく新規セッションを作成し、セッションIDを返す。"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")

    session_id = str(uuid.uuid4())
    title = body.title or body.file_path.split("/")[-1]

    _write_session_meta(agent.path, session_id, body.file_path, title)
    _write_link(agent.path, body.file_path, {
        "file_path": body.file_path,
        "session_id": session_id,
    })

    print(f"[FILE-LINK] 作成 file={body.file_path} sid={session_id}", flush=True)
    return {"session_id": session_id}
