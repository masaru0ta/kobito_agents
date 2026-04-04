"""チャット関連API"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.config import AgentNotFoundError, ConfigManager
from server.session_reader import SessionReader
from server.cli_bridge import CLIBridge, parse_stream_event, resolve_model
from server.task_manager import TaskManager
from server.task_context import build_task_context
from server.routes.deps import get_config_manager, get_session_reader, get_cli_bridge, get_startup_id

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["chat"])


def _update_session_meta(agent_path: str, session_id: str, updates: dict) -> dict:
    """セッションメタデータを読み込み、更新して書き戻す。更新後のメタデータを返す。"""
    meta_dir = Path(agent_path) / ".kobito" / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / f"{session_id}.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(updates)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


class CLILaunchRequest(BaseModel):
    session_id: str | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    model_tier: str | None = None
    task_id: str | None = None
    task_mode: str | None = None


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

    tier = body.model_tier or agent.model_tier
    model = resolve_model(agent.cli, tier)

    # タスクコンテキスト注入
    prompt = body.message
    if body.task_id:
        tm = TaskManager(agent.path)
        try:
            task = tm.get_task(body.task_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"タスク '{body.task_id}' が見つかりません")
        context = build_task_context(task, body.task_mode or "work")
        prompt = context + "\n\n" + body.message

    async def event_stream():
        accumulated_text = ""
        got_result = False
        try:
            stream = bridge.run_stream(
                project_path=agent.path,
                prompt=prompt,
                model=model,
                session_id=body.session_id,
            )
            async for raw_event in stream:
                # _ping は接続維持のためのダミーイベント（15秒無応答時にrun_stream側で生成）
                if raw_event.get("type") == "_ping":
                    yield ": ping\n\n"
                    continue
                ev = parse_stream_event(raw_event)
                if ev.event_type == "assistant" and ev.text:
                    accumulated_text += ev.text
                    yield f"data: {json.dumps({'type': 'chunk', 'data': accumulated_text}, ensure_ascii=False)}\n\n"
                    for tu in ev.tool_uses:
                        desc = tu.get("name", "")
                        inp = tu.get("input", {})
                        if inp.get("file_path"):
                            desc += f": {inp['file_path'].split('/')[-1].split(chr(92))[-1]}"
                        elif inp.get("command"):
                            desc += f": {inp['command'][:60]}"
                        yield f"data: {json.dumps({'type': 'tool_use', 'data': desc}, ensure_ascii=False)}\n\n"
                elif ev.event_type == "result":
                    got_result = True
                    yield f"data: {json.dumps({'type': 'session_id', 'data': ev.session_id}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            print(f"[STREAM] 例外発生 agent={agent_id} sid={body.session_id}: {type(e).__name__}: {e}", flush=True)
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)}, ensure_ascii=False)}\n\n"
            return

        if not got_result:
            # プロセス異常終了などでresultイベントが来なかった場合（サーバー再起動時を含む）
            print(f"[STREAM] got_result=False agent={agent_id} sid={body.session_id} (プロセス異常終了またはサーバー再起動)", flush=True)
            yield f"data: {json.dumps({'type': 'error', 'data': 'サーバーが再起動されました。再度送信してください。'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class TitleRequest(BaseModel):
    title: str


@router.put("/sessions/{session_id}/title")
def update_session_title(
    agent_id: str,
    session_id: str,
    body: TitleRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    _update_session_meta(agent.path, session_id, {"title": body.title})
    return {"status": "ok", "title": body.title}


class ModelTierRequest(BaseModel):
    model_tier: str


@router.put("/sessions/{session_id}/model-tier")
def update_session_model_tier(
    agent_id: str,
    session_id: str,
    body: ModelTierRequest,
    config: ConfigManager = Depends(get_config_manager),
):
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    _update_session_meta(agent.path, session_id, {"model_tier": body.model_tier})
    return {"status": "ok"}


@router.get("/process-status")
def get_process_status(
    agent_id: str,
    watching: str | None = None,
    config: ConfigManager = Depends(get_config_manager),
    bridge: CLIBridge = Depends(get_cli_bridge),
    reader: SessionReader = Depends(get_session_reader),
    startup_id: str = Depends(get_startup_id),
):
    """稼働中のセッションプロセス一覧 + 更新検知情報を返す"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    result = {
        "startup_id": startup_id,
        "inferring": bridge.inferring_session_ids(agent.path),
        "dir_mtime": reader.get_dir_mtime(agent.path),
        "processes": bridge.process_debug_info(agent.path),
    }
    if watching:
        result["watching_mtime"] = reader.get_session_mtime(agent.path, watching)
    return result


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
    _update_session_meta(agent.path, session_id, {"hidden": True})
    return {"status": "ok"}


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    agent_id: str,
    session_id: str,
    config: ConfigManager = Depends(get_config_manager),
    bridge: CLIBridge = Depends(get_cli_bridge),
):
    """推論中のセッションプロセスを強制停止する"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    stopped = await bridge.stop_session(agent.path, session_id)
    return {"status": "ok", "stopped": stopped}


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
