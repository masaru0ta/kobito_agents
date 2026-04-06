"""Webサーバー — FastAPIアプリケーション"""

from __future__ import annotations

import os
import signal
import uuid
from contextlib import asynccontextmanager
from pathlib import Path


from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.config import ConfigManager
from server.session_reader import SessionReader, ClaudeSessionReader
from server.cli_bridge import CLIBridge, cleanup_orphaned_processes
from server.routes.agents import router as agents_router
from server.routes.chat import router as chat_router
from server.routes.tasks import router as tasks_router
from server.routes.reports import router as reports_router
from server.routes.file_links import router as file_links_router
from server.routes.scheduler import router as scheduler_router
from server.routes.internal import router as internal_router
from server.scheduler import Scheduler


def resolve_project_root() -> Path:
    """プロジェクトルートを返す（server/app.py → server → src → プロジェクトルート）"""
    return Path(__file__).resolve().parent.parent.parent


def create_app(
    config_manager: ConfigManager | None = None,
    session_reader: SessionReader | None = None,
    cli_bridge: CLIBridge | None = None,
) -> FastAPI:
    if config_manager is None:
        project_root = resolve_project_root()
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_manager = ConfigManager(data_dir=data_dir, system_path=str(project_root))

    if session_reader is None:
        session_reader = ClaudeSessionReader()

    if cli_bridge is None:
        cli_bridge = CLIBridge()

    scheduler = Scheduler(config_manager=config_manager, cli_bridge=cli_bridge)
    startup_id = str(uuid.uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        print(f"[SERVER] 起動 startup_id={startup_id}", flush=True)
        # 起動時: 前回サーバーの孤児プロセスをクリーンアップ
        for agent in config_manager.list_agents():
            cleanup_orphaned_processes(agent.path)
        # スケジューラー タイマーループ開始
        scheduler.start()
        yield
        print(f"[SERVER] 終了 startup_id={startup_id}", flush=True)
        await scheduler.stop()
        await cli_bridge.shutdown()

    app = FastAPI(title="kobito_agents", lifespan=lifespan)

    app.state.config_manager = config_manager
    app.state.session_reader = session_reader
    app.state.cli_bridge = cli_bridge
    app.state.scheduler = scheduler
    app.state.startup_id = startup_id

    @app.post("/api/restart")
    async def restart_server():
        """サーバーを再起動する。ラッパースクリプトが再起動を担う。"""
        await cli_bridge.shutdown()
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "restarting"}

    app.include_router(agents_router)
    app.include_router(chat_router)
    app.include_router(tasks_router)
    app.include_router(reports_router)
    app.include_router(file_links_router)
    app.include_router(scheduler_router)
    app.include_router(internal_router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# uvicornから参照されるモジュールレベル変数
app = create_app()
