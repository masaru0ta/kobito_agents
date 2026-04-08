"""Webサーバー — FastAPIアプリケーション"""

from __future__ import annotations

import logging
import os
import re
import signal
import uuid
from contextlib import asynccontextmanager
from pathlib import Path


from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.config import ConfigManager
from server.session_reader import SessionReader, AgentSessionReader
from server.cli_bridge import CLIBridge, cleanup_orphaned_processes
from server.routes.agents import router as agents_router
from server.routes.chat import router as chat_router
from server.routes.tasks import router as tasks_router
from server.routes.reports import router as reports_router
from server.routes.file_links import router as file_links_router
from server.routes.scheduler import router as scheduler_router
from server.routes.internal import router as internal_router
from server.routes.teams import router as teams_router
from server.scheduler import Scheduler


# ANSI カラーコード
_C = {
    "reset":   "\033[0m",
    "dim":     "\033[2m",
    "cyan":    "\033[36m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "red":     "\033[31m",
    "bold":    "\033[1m",
    "magenta": "\033[35m",
    "white":   "\033[97m",
}

_LEVEL_COLOR = {
    logging.DEBUG:    _C["dim"],
    logging.INFO:     "",
    logging.WARNING:  _C["yellow"],
    logging.ERROR:    _C["red"],
    logging.CRITICAL: _C["bold"] + _C["red"],
}

# モジュール名ごとの強調色
_MODULE_COLOR = {
    "server.cli_bridge": _C["cyan"],
    "server.scheduler":  _C["green"],
}


_CHAT_LOG_RE   = re.compile(r'^(チャット受信|チャンク受信) agent=(.+?) (「.+」)(.*)?$')
_TOOL_LOG_RE   = re.compile(r'^ツール実行 agent=(.+?) (.+?) sid=(\S+)$')
_PROCESS_KW_RE = re.compile(r'^(プロセス起動|アイドルタイムアウトによりプロセス終了|プロセス異常終了を検出|モデル変更検出[^:]*)')

# モジュール名を出力しないロガー（メッセージ自体で文脈が明らか）
_NO_MODULE_NAME = {"server.cli_bridge", "server.routes.chat"}


class _ColoredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        lc = _LEVEL_COLOR.get(record.levelno, "")
        mc = _MODULE_COLOR.get(record.name, "")
        ts  = self.formatTime(record, self.datefmt)
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        name_part = f"{mc}[{record.name}]{_C['reset']}" if mc else f"[{record.name}]"

        # チャット受信・チャンク受信ログ
        cm = _CHAT_LOG_RE.match(msg)
        if cm:
            label_color = _C['green'] if cm.group(1) == "チャット受信" else _C['dim']
            extra = cm.group(4) or ""
            msg_part = (
                f"{_C['bold']}{label_color}{cm.group(1)}{_C['reset']} "
                f"agent={_C['cyan']}{cm.group(2)}{_C['reset']} "
                f"{_C['yellow']}{cm.group(3)}{_C['reset']}"
                f"{_C['dim']}{extra}{_C['reset']}"
            )
            return f"{_C['dim']}{ts}{_C['reset']} {msg_part}"

        # ツール実行ログ
        tm = _TOOL_LOG_RE.match(msg)
        if tm:
            msg_part = (
                f"{_C['dim']}ツール実行{_C['reset']} "
                f"agent={_C['cyan']}{tm.group(1)}{_C['reset']} "
                f"{_C['magenta']}{tm.group(2)}{_C['reset']} "
                f"{_C['dim']}sid={tm.group(3)}{_C['reset']}"
            )
            return f"{_C['dim']}{ts}{_C['reset']} {msg_part}"

        # cli_bridge のプロセスイベントログ
        pm = _PROCESS_KW_RE.match(msg)
        if pm:
            rest = msg[pm.end():]
            msg_part = f"{_C['bold']}{_C['cyan']}{pm.group(1)}{_C['reset']}{rest}"
            return f"{_C['dim']}{ts}{_C['reset']} {msg_part}"

        msg_part = f"{lc}{msg}{_C['reset']}" if lc else msg
        if record.name in _NO_MODULE_NAME:
            return f"{_C['dim']}{ts}{_C['reset']} {msg_part}"
        return f"{_C['dim']}{ts}{_C['reset']} {name_part} {msg_part}"


_LOG_FMT = _ColoredFormatter(datefmt="%Y-%m-%d %H:%M:%S")

# アクセスログ用: "POST /api/agents/system/chat 200" のみ出力
_ACCESS_EXTRACT = re.compile(r'"((?:GET|POST|PUT|DELETE|PATCH|HEAD) \S+)[^"]*" (\d{3})')

_STATUS_COLOR  = {"2": _C["dim"], "4": _C["yellow"], "5": _C["red"]}


class _AccessLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        m = _ACCESS_EXTRACT.search(msg)
        if m:
            method_path = m.group(1)
            status = m.group(2)
            sc = _STATUS_COLOR.get(status[0], "")
            ts = self.formatTime(record, self.datefmt)
            status_part = f"{sc}{status}{_C['reset']}" if sc else status
            return f"{_C['dim']}{ts}{_C['reset']} {method_path} {status_part}"
        return f"{self.formatTime(record, self.datefmt)} {msg}"


logger = logging.getLogger(__name__)


class _AccessLogFilter(logging.Filter):
    """静的ファイルと高頻度ポーリングのアクセスログを抑制する"""
    _SKIP = re.compile(
        r'"(?:GET|HEAD) /(?:'
        r'[^"]+\.(?:css|js|ico|png|jpg|gif|webp|woff2?|ttf|svg|map)'  # 静的ファイル
        r'|api/[^/]+/[^/]+/process-status[^"]*'  # プロセス状態ポーリング
        r'|api/[^/]+/[^/]+/tasks[^"]*'           # タスク一覧ポーリング
        r'|api/scheduler/status[^"]*'             # スケジューラー状態ポーリング
        r') HTTP'
    )

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._SKIP.search(record.getMessage())


def _setup_logging() -> None:
    """ルートロガー初期化。uvicornハンドラはこの時点で未設定のため後でパッチする。"""
    handler = logging.StreamHandler()
    handler.setFormatter(_LOG_FMT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # フィルタはロガーオブジェクト自体に付与するためハンドラ追加後も有効
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())


def _patch_uvicorn_logging() -> None:
    """uvicorn起動後にフォーマットを統一する（lifespan内から呼ぶ）"""
    access_fmt = _AccessLogFormatter(datefmt="%Y-%m-%d %H:%M:%S")
    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.setFormatter(access_fmt)
    for name in ("uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(_LOG_FMT)


_setup_logging()


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
        session_reader = AgentSessionReader()

    if cli_bridge is None:
        cli_bridge = CLIBridge()

    scheduler = Scheduler(config_manager=config_manager, cli_bridge=cli_bridge)
    startup_id = str(uuid.uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _patch_uvicorn_logging()
        logger.info("サーバー起動 port=8200")
        # 起動時: 前回サーバーの孤児プロセスをクリーンアップ
        for agent in config_manager.list_agents():
            cleanup_orphaned_processes(agent.path)
        # スケジューラー タイマーループ開始
        scheduler.start()
        yield
        logger.info("サーバー終了")
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
    app.include_router(teams_router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# uvicornから参照されるモジュールレベル変数
app = create_app()
