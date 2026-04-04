"""推論中判定のテスト

対象:
- PIDファイル管理 (_pid_dir, _write_pid_file, _remove_pid_file)
- TCP接続検査 (_has_api_connection)
- 孤児プロセスクリーンアップ (cleanup_orphaned_processes)
- CLIBridge.inferring_session_ids()
- CLIBridge.shutdown()
- GET /process-status エンドポイント
"""

import asyncio
import os
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.cli_bridge import (
    CLIBridge,
    ManagedProcess,
    _pid_dir,
    _has_api_connection,
    _judge_inferring,
    _remove_pid_file,
    _write_pid_file,
    cleanup_orphaned_processes,
)


# ============================================================
# ヘルパー
# ============================================================

def _make_pid_file(project_path: str, session_id: str, pid: int) -> Path:
    """テスト用PIDファイルを直接作成する"""
    d = Path(project_path) / ".kobito" / "alive"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.pid"
    p.write_text(str(pid), encoding="utf-8")
    return p


def _pid_file_path(project_path: str, session_id: str) -> Path:
    return Path(project_path) / ".kobito" / "alive" / f"{session_id}.pid"


def _make_managed_process(
    pid: int = 1000,
    alive: bool = True,
    session_id: str = "sess-001",
    project_path: str = "",
    model: str = "opus",
    message_sent_at: float = 1.0,
) -> ManagedProcess:
    """テスト用ManagedProcessをモックで作成する"""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None if alive else 0
    mp = ManagedProcess.__new__(ManagedProcess)
    mp.proc = proc
    mp.queue = asyncio.Queue()
    mp.lock = asyncio.Lock()
    mp.reader_thread = None
    mp.session_id = session_id
    mp.model = model
    mp.project_path = project_path
    mp.last_used = 0
    mp.message_sent_at = message_sent_at
    mp.last_seen_jsonl_mtime = 0.0
    mp.last_mtime_change_at = 0.0
    mp._loop = None
    return mp


# psutil のモック用名前付きタプル
ConnAddr = namedtuple("ConnAddr", ["ip", "port"])
ConnInfo = namedtuple("ConnInfo", ["raddr", "status"])


# ============================================================
# PID ファイル管理
# ============================================================

class TestAliveDir:
    """_pid_dir: PIDファイルディレクトリの作成"""

    def test_ディレクトリが作成される(self, tmp_path):
        project = str(tmp_path / "project")
        os.makedirs(project)
        d = _pid_dir(project)
        assert d.exists()
        assert d == Path(project) / ".kobito" / "alive"

    def test_既に存在しても例外にならない(self, tmp_path):
        project = str(tmp_path / "project")
        os.makedirs(project)
        _pid_dir(project)
        d = _pid_dir(project)  # 2回目
        assert d.exists()


class TestWritePidFile:
    """_write_pid_file: PIDファイルの書き込み"""

    def test_PIDファイルが作成される(self, tmp_path):
        project = str(tmp_path)
        _write_pid_file(project, "sess-001", 12345)
        p = _pid_file_path(project, "sess-001")
        assert p.exists()
        assert p.read_text(encoding="utf-8") == "12345"

    def test_session_idが空の場合は書き込まない(self, tmp_path):
        project = str(tmp_path)
        _write_pid_file(project, "", 12345)
        d = Path(project) / ".kobito" / "alive"
        assert not d.exists() or len(list(d.glob("*.pid"))) == 0

    def test_new_プレフィックスの場合は書き込まない(self, tmp_path):
        project = str(tmp_path)
        _write_pid_file(project, "new-12345678", 12345)
        d = Path(project) / ".kobito" / "alive"
        assert not d.exists() or len(list(d.glob("*.pid"))) == 0

    def test_上書きできる(self, tmp_path):
        project = str(tmp_path)
        _write_pid_file(project, "sess-001", 100)
        _write_pid_file(project, "sess-001", 200)
        p = _pid_file_path(project, "sess-001")
        assert p.read_text(encoding="utf-8") == "200"


class TestRemovePidFile:
    """_remove_pid_file: PIDファイルの削除"""

    def test_PIDファイルが削除される(self, tmp_path):
        project = str(tmp_path)
        _write_pid_file(project, "sess-001", 12345)
        _remove_pid_file(project, "sess-001")
        assert not _pid_file_path(project, "sess-001").exists()

    def test_存在しないファイルでも例外にならない(self, tmp_path):
        project = str(tmp_path)
        _remove_pid_file(project, "nonexistent")  # 例外が出ないこと

    def test_session_idが空の場合は何もしない(self, tmp_path):
        project = str(tmp_path)
        _remove_pid_file(project, "")  # 例外が出ないこと


# ============================================================
# TCP 接続検査
# ============================================================

class TestHasApiConnection:
    """_has_api_connection: TCP port 443 接続の検査"""

    def test_port443にESTABLISHED接続があればTrue(self):
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_proc.connections.return_value = [
            ConnInfo(raddr=ConnAddr("1.2.3.4", 443), status="ESTABLISHED"),
        ]
        with patch("server.cli_bridge.psutil.Process", return_value=mock_proc):
            assert _has_api_connection(1000) is True

    def test_port443以外の接続ではFalse(self):
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_proc.connections.return_value = [
            ConnInfo(raddr=ConnAddr("1.2.3.4", 80), status="ESTABLISHED"),
        ]
        with patch("server.cli_bridge.psutil.Process", return_value=mock_proc):
            assert _has_api_connection(1000) is False

    def test_接続がなければFalse(self):
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_proc.connections.return_value = []
        with patch("server.cli_bridge.psutil.Process", return_value=mock_proc):
            assert _has_api_connection(1000) is False

    def test_ESTABLISHED以外のステータスではFalse(self):
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_proc.connections.return_value = [
            ConnInfo(raddr=ConnAddr("1.2.3.4", 443), status="TIME_WAIT"),
        ]
        with patch("server.cli_bridge.psutil.Process", return_value=mock_proc):
            assert _has_api_connection(1000) is False

    def test_子プロセスの接続も検査する(self):

        mock_parent = MagicMock()
        mock_parent.connections.return_value = []

        mock_child = MagicMock()
        mock_child.connections.return_value = [
            ConnInfo(raddr=ConnAddr("1.2.3.4", 443), status="ESTABLISHED"),
        ]
        mock_parent.children.return_value = [mock_child]

        with patch("server.cli_bridge.psutil.Process", return_value=mock_parent):
            assert _has_api_connection(1000) is True

    def test_プロセスが存在しなければFalse(self):
        import psutil as _psutil
        with patch("server.cli_bridge.psutil.Process", side_effect=_psutil.NoSuchProcess(1000)):
            assert _has_api_connection(1000) is False

    def test_アクセス拒否でもFalse(self):
        import psutil as _psutil
        with patch("server.cli_bridge.psutil.Process", side_effect=_psutil.AccessDenied(1000)):
            assert _has_api_connection(1000) is False

    def test_子プロセスのAccessDeniedはスキップして親を検査(self):
        import psutil as _psutil

        mock_child = MagicMock()
        mock_child.connections.side_effect = _psutil.AccessDenied(2000)

        mock_parent = MagicMock()
        mock_parent.connections.return_value = [
            ConnInfo(raddr=ConnAddr("1.2.3.4", 443), status="ESTABLISHED"),
        ]
        mock_parent.children.return_value = [mock_child]

        with patch("server.cli_bridge.psutil.Process", return_value=mock_parent):
            assert _has_api_connection(1000) is True

    def test_raddrがNoneの接続は無視する(self):
        mock_proc = MagicMock()
        mock_proc.children.return_value = []
        mock_proc.connections.return_value = [
            ConnInfo(raddr=None, status="LISTEN"),
        ]
        with patch("server.cli_bridge.psutil.Process", return_value=mock_proc):
            assert _has_api_connection(1000) is False


# ============================================================
# 孤児プロセスクリーンアップ
# ============================================================

class TestCleanupOrphanedProcesses:
    """cleanup_orphaned_processes: サーバー起動時の孤児プロセス処理"""

    def test_aliveディレクトリがなければ何もしない(self, tmp_path):
        project = str(tmp_path / "project")
        os.makedirs(project)
        cleanup_orphaned_processes(project)  # 例外が出ないこと

    def test_死亡プロセスのPIDファイルを削除する(self, tmp_path):
        project = str(tmp_path)
        _make_pid_file(project, "sess-dead", 99999)

        with patch("server.pid_manager.is_process_alive", return_value=False):
            cleanup_orphaned_processes(project)

        assert not _pid_file_path(project, "sess-dead").exists()

    def test_生存_推論終了のプロセスをterminateしてPIDファイルを削除する(self, tmp_path):
        project = str(tmp_path)
        _make_pid_file(project, "sess-idle", 5000)

        with (
            patch("server.pid_manager.is_process_alive", return_value=True),
            patch("server.cli_bridge._has_api_connection", return_value=False),
            patch("server.pid_manager.terminate_process") as mock_terminate,
        ):
            cleanup_orphaned_processes(project)

        mock_terminate.assert_called_once_with(5000)
        assert not _pid_file_path(project, "sess-idle").exists()

    def test_生存_推論中のプロセスは残す(self, tmp_path):
        project = str(tmp_path)
        _make_pid_file(project, "sess-inferring", 6000)

        with (
            patch("server.pid_manager.is_process_alive", return_value=True),
            patch("server.cli_bridge._has_api_connection", return_value=True),
        ):
            cleanup_orphaned_processes(project)

        # PIDファイルは残る
        assert _pid_file_path(project, "sess-inferring").exists()

    def test_不正なPIDファイルを削除する(self, tmp_path):
        project = str(tmp_path)
        d = Path(project) / ".kobito" / "alive"
        d.mkdir(parents=True)
        bad_file = d / "sess-bad.pid"
        bad_file.write_text("not-a-number", encoding="utf-8")

        cleanup_orphaned_processes(project)

        assert not bad_file.exists()

    def test_複数プロセスを個別に判定する(self, tmp_path):
        project = str(tmp_path)
        _make_pid_file(project, "sess-alive", 1001)
        _make_pid_file(project, "sess-dead", 1002)
        _make_pid_file(project, "sess-inferring", 1003)

        def alive_side_effect(pid):
            return pid != 1002  # 1002は死亡

        def api_conn_side_effect(pid):
            return pid == 1003  # 1003のみ推論中

        with (
            patch("server.pid_manager.is_process_alive", side_effect=alive_side_effect),
            patch("server.cli_bridge._has_api_connection", side_effect=api_conn_side_effect),
            patch("server.pid_manager.terminate_process") as mock_terminate,
        ):
            cleanup_orphaned_processes(project)

        # 死亡(1002) → PIDファイル削除
        assert not _pid_file_path(project, "sess-dead").exists()
        # 生存+推論終了(1001) → terminate + PIDファイル削除
        assert not _pid_file_path(project, "sess-alive").exists()
        mock_terminate.assert_called_once_with(1001)
        # 生存+推論中(1003) → 残す
        assert _pid_file_path(project, "sess-inferring").exists()


# ============================================================
# CLIBridge.inferring_session_ids
# ============================================================

class TestInferringSessionIds:
    """CLIBridge.inferring_session_ids: プール内+孤児の推論中判定"""

    def test_プール内の推論中セッションを返す(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        with patch("server.cli_bridge._has_api_connection", return_value=True):
            result = bridge.inferring_session_ids(project)

        assert result == ["sess-001"]

    def test_プール内の推論終了セッションは返さない(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        with patch("server.cli_bridge._has_api_connection", return_value=False):
            result = bridge.inferring_session_ids(project)

        assert result == []

    def test_死亡プロセスは検査しない(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, alive=False, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        with patch("server.cli_bridge._has_api_connection") as mock_api:
            result = bridge.inferring_session_ids(project)

        assert result == []
        mock_api.assert_not_called()

    def test_new_プレフィックスのセッションは除外(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="", project_path=project)
        bridge._pool[f"{project}::new-12345"] = mp

        with patch("server.cli_bridge._has_api_connection", return_value=True):
            result = bridge.inferring_session_ids(project)

        assert result == []

    def test_別プロジェクトのセッションは含まない(self, tmp_path):
        project_a = str(tmp_path / "a")
        project_b = str(tmp_path / "b")
        os.makedirs(project_a)
        os.makedirs(project_b)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project_b)
        bridge._pool[f"{project_b}::sess-001"] = mp

        with patch("server.cli_bridge._has_api_connection", return_value=True):
            result = bridge.inferring_session_ids(project_a)

        assert result == []

    def test_PIDファイルの孤児プロセスが推論中なら返す(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()  # プール空

        _make_pid_file(project, "sess-orphan", 9000)

        with (
            patch("server.cli_bridge.is_process_alive", return_value=True),
            patch("server.cli_bridge._has_api_connection", return_value=True),
        ):
            result = bridge.inferring_session_ids(project)

        assert result == ["sess-orphan"]

    def test_PIDファイルの孤児プロセスが推論終了ならkillして返さない(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        _make_pid_file(project, "sess-orphan", 9000)

        with (
            patch("server.cli_bridge.is_process_alive", return_value=True),
            patch("server.cli_bridge._has_api_connection", return_value=False),
            patch("server.cli_bridge.terminate_process") as mock_terminate,
        ):
            result = bridge.inferring_session_ids(project)

        assert result == []
        mock_terminate.assert_called_once_with(9000)
        assert not _pid_file_path(project, "sess-orphan").exists()

    def test_PIDファイルの死亡プロセスはPIDファイルを削除して返さない(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        _make_pid_file(project, "sess-dead", 9000)

        with patch("server.cli_bridge.is_process_alive", return_value=False):
            result = bridge.inferring_session_ids(project)

        assert result == []
        assert not _pid_file_path(project, "sess-dead").exists()

    def test_プール内で検査済みのセッションはPIDファイル走査でスキップ(self, tmp_path):
        """プール内にあるセッションはPIDファイル側で二重検査しない"""
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        # PIDファイルも存在する（正常運用で起こりうる状態）
        _make_pid_file(project, "sess-001", 1000)

        call_count = 0
        def count_api_calls(pid):
            nonlocal call_count
            call_count += 1
            return True

        with patch("server.cli_bridge._has_api_connection", side_effect=count_api_calls):
            result = bridge.inferring_session_ids(project)

        # プール側で1回だけ呼ばれ、PIDファイル側では呼ばれない
        assert call_count == 1
        assert result == ["sess-001"]

    def test_プールと孤児の両方から推論中セッションを集約する(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        # プール内: sess-001 が推論中
        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        # 孤児: sess-002 が推論中
        _make_pid_file(project, "sess-002", 2000)

        with (
            patch("server.cli_bridge._has_api_connection", return_value=True),
            patch("server.cli_bridge.is_process_alive", return_value=True),
        ):
            result = bridge.inferring_session_ids(project)

        assert set(result) == {"sess-001", "sess-002"}


# ============================================================
# CLIBridge.shutdown
# ============================================================

class TestShutdown:
    """CLIBridge.shutdown: サーバー停止時の推論完了待機"""

    @pytest.mark.asyncio
    async def test_推論中でなければ即座に終了する(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        with patch("server.cli_bridge._has_api_connection", return_value=False):
            await bridge.shutdown()

        mp.proc.terminate.assert_called()
        assert len(bridge._pool) == 0

    @pytest.mark.asyncio
    async def test_推論完了を待ってから終了する(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp

        # 最初の2回は推論中、3回目で完了
        call_count = 0
        def api_conn(pid):
            nonlocal call_count
            call_count += 1
            return call_count <= 2

        with patch("server.cli_bridge._has_api_connection", side_effect=api_conn):
            await bridge.shutdown()

        assert call_count == 3
        assert len(bridge._pool) == 0

    @pytest.mark.asyncio
    async def test_PIDファイルが削除される(self, tmp_path):
        project = str(tmp_path)
        bridge = CLIBridge()

        mp = _make_managed_process(pid=1000, session_id="sess-001", project_path=project)
        bridge._pool[f"{project}::sess-001"] = mp
        _make_pid_file(project, "sess-001", 1000)

        with patch("server.cli_bridge._has_api_connection", return_value=False):
            await bridge.shutdown()

        assert not _pid_file_path(project, "sess-001").exists()

    @pytest.mark.asyncio
    async def test_cleanupタスクをキャンセルする(self):
        bridge = CLIBridge()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        bridge._cleanup_task = mock_task

        with patch("server.cli_bridge._has_api_connection", return_value=False):
            await bridge.shutdown()

        mock_task.cancel.assert_called_once()


# ============================================================
# 推論判定ロジック (_judge_inferring)
# ============================================================

class TestJudgeInferring:
    """_judge_inferring: ロジック表に基づく推論中判定"""

    def test_未送信なら推論中ではない(self):
        assert _judge_inferring(
            awaiting_response=False, has_connection=True,
            jsonl_updated=False, last_role=None, jsonl_stable=False,
        ) is False

    def test_TCP切れなら推論完了(self):
        assert _judge_inferring(
            awaiting_response=True, has_connection=False,
            jsonl_updated=False, last_role=None, jsonl_stable=False,
        ) is False

    def test_接続中_JSONL未更新なら推論中(self):
        assert _judge_inferring(
            awaiting_response=True, has_connection=True,
            jsonl_updated=False, last_role=None, jsonl_stable=False,
        ) is True

    def test_接続中_JSONL末尾userなら推論中(self):
        assert _judge_inferring(
            awaiting_response=True, has_connection=True,
            jsonl_updated=True, last_role="user", jsonl_stable=True,
        ) is True

    def test_接続中_JSONL末尾assistant_未安定なら推論中(self):
        assert _judge_inferring(
            awaiting_response=True, has_connection=True,
            jsonl_updated=True, last_role="assistant", jsonl_stable=False,
        ) is True

    def test_接続中_JSONL末尾assistant_安定なら推論完了(self):
        assert _judge_inferring(
            awaiting_response=True, has_connection=True,
            jsonl_updated=True, last_role="assistant", jsonl_stable=True,
        ) is False


# ============================================================
# process-status APIエンドポイント
# ============================================================

class TestProcessStatusAPI:
    """GET /api/agents/{id}/process-status エンドポイント"""

    @pytest.fixture
    def mock_app(self):
        from server.app import create_app
        from server.config import AgentInfo

        mock_config = MagicMock()
        mock_reader = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge.shutdown = AsyncMock()

        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp/project",
            cli="claude", model_tier="deep", system_prompt="",
        )

        app = create_app(
            config_manager=mock_config,
            session_reader=mock_reader,
            cli_bridge=mock_bridge,
        )

        return app, mock_config, mock_reader, mock_bridge

    def test_inferringフィールドに推論中セッションが返る(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, _, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = ["sess-001", "sess-002"]

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["inferring"] == ["sess-001", "sess-002"]

    def test_推論中セッションがなければ空リスト(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, _, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        data = resp.json()
        assert data["inferring"] == []

    def test_startup_idが返る(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, _, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        data = resp.json()
        assert "startup_id" in data
        assert isinstance(data["startup_id"], str)
        assert len(data["startup_id"]) > 0

    def test_dir_mtimeが返る(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, mock_reader, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []
        mock_reader.get_dir_mtime.return_value = 1234567890.123

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        data = resp.json()
        assert data["dir_mtime"] == 1234567890.123

    def test_watchingパラメータでwatching_mtimeが返る(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, mock_reader, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []
        mock_reader.get_session_mtime.return_value = 9876543210.456

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status?watching=sess-001")

        data = resp.json()
        assert data["watching_mtime"] == 9876543210.456

    def test_watchingパラメータなしではwatching_mtimeが含まれない(self, mock_app):
        from fastapi.testclient import TestClient

        app, _, _, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        data = resp.json()
        assert "watching_mtime" not in data

    def test_activeフィールドは存在しない(self, mock_app):
        """旧仕様のactiveフィールドが返らないことを確認"""
        from fastapi.testclient import TestClient

        app, _, _, mock_bridge = mock_app
        mock_bridge.inferring_session_ids.return_value = []

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/process-status")

        data = resp.json()
        assert "active" not in data
        assert "responding" not in data

    def test_存在しないエージェントで404(self, mock_app):
        from fastapi.testclient import TestClient
        from server.config import AgentNotFoundError

        app, mock_config, _, _ = mock_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not found")

        with TestClient(app) as client:
            resp = client.get("/api/agents/nonexistent/process-status")

        assert resp.status_code == 404
