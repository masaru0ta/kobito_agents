"""Webサーバー — FastAPIアプリケーション"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.config import ConfigManager
from server.session_reader import SessionReader, ClaudeSessionReader
from server.cli_bridge import CLIBridge
from server.routes.agents import router as agents_router
from server.routes.chat import router as chat_router


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await cli_bridge.shutdown()

    app = FastAPI(title="kobito_agents", lifespan=lifespan)

    app.state.config_manager = config_manager
    app.state.session_reader = session_reader
    app.state.cli_bridge = cli_bridge

    app.include_router(agents_router)
    app.include_router(chat_router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# uvicornから参照されるモジュールレベル変数
app = create_app()
