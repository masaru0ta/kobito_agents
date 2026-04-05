"""PIDファイル管理 — セッションごとのプロセス追跡と孤児プロセスのクリーンアップ"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def pid_dir(project_path: str) -> Path:
    """PID ファイル置き場: {project}/.kobito/alive/"""
    d = Path(project_path) / ".kobito" / "alive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pid_file(project_path: str, session_id: str, pid: int) -> None:
    """プロセス起動時に PID を記録する"""
    if not session_id or session_id.startswith("new-"):
        return
    path = pid_dir(project_path) / f"{session_id}.pid"
    path.write_text(str(pid), encoding="utf-8")


def remove_pid_file(project_path: str, session_id: str) -> None:
    """プロセス終了時に PID ファイルを削除する"""
    if not session_id:
        return
    path = pid_dir(project_path) / f"{session_id}.pid"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def iter_pid_files(project_path: str) -> list[tuple[Path, str, int]]:
    """PIDファイルを走査し、(ファイルパス, session_id, pid) のリストを返す。
    不正なファイル（パースできない等）は削除してスキップする。"""
    d = Path(project_path) / ".kobito" / "alive"
    if not d.exists():
        return []
    result = []
    for pid_file in d.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            result.append((pid_file, pid_file.stem, pid))
        except (ValueError, OSError):
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass
    return result


def is_process_alive(pid: int) -> bool:
    """プロセスが生存しているか確認する"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def terminate_process(pid: int) -> None:
    """プロセスを安全にterminateする"""
    try:
        psutil.Process(pid).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def cleanup_orphaned_processes(project_path: str, has_api_connection) -> None:
    """サーバー起動時に呼ぶ。PID ファイルから孤児プロセスを検出し、
    推論が終わっているものは kill してクリーンアップする。

    Args:
        project_path: プロジェクトのパス
        has_api_connection: pid を受け取り、API接続があるかを返す callable
    """
    for pid_file, sid, pid in iter_pid_files(project_path):
        if not is_process_alive(pid):
            # プロセスが既に死んでいる → PIDファイル削除
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            continue

        if not has_api_connection(pid):
            # 推論終了済み → kill
            terminate_process(pid)
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            logger.info(f"孤児プロセス終了: PID={pid} session={sid}")
        else:
            logger.info(f"孤児プロセス推論中: PID={pid} session={sid}")
