"""依存性注入"""

from fastapi import Request

from server.config import ConfigManager
from server.session_reader import SessionReader
from server.cli_bridge import CLIBridge


def get_config_manager(request: Request) -> ConfigManager:
    return request.app.state.config_manager


def get_session_reader(request: Request) -> SessionReader:
    return request.app.state.session_reader


def get_cli_bridge(request: Request) -> CLIBridge:
    return request.app.state.cli_bridge


def get_startup_id(request: Request) -> str:
    return request.app.state.startup_id
