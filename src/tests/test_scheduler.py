"""Phase3 スケジューラーエンジンのテスト"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo


# ================================================================
# 共通ヘルパー / フィクスチャ
# ================================================================

TASK_APPROVED_DRAFT = """\
---
title: 承認済みタスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
---

## 作業ステップ

- [ ] ステップ1
"""

TASK_APPROVED_DONE = """\
---
title: 完了タスク
agent: system
phase: done
created: 2026-04-01T17:00:00Z
---

## 完了済み
"""

TASK_PENDING = """\
---
title: 未承認タスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
---

## 概要

未承認。
"""


def _setup_project(tmp_path):
    """プロジェクトディレクトリの基本構造を作成する"""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# test", encoding="utf-8")
    (project_dir / "tasks").mkdir()
    (project_dir / ".kobito" / "tasks").mkdir(parents=True)
    (project_dir / ".kobito" / "meta").mkdir(parents=True)
    return project_dir


def _create_task(project_dir: Path, task_id: str, content: str, approval: str = "pending"):
    """タスクファイルとメタデータを作成する"""
    (project_dir / "tasks" / f"{task_id}.md").write_text(content, encoding="utf-8")
    meta = {
        "task_id": task_id,
        "approval": approval,
        "approved_at": "2026-04-01T00:00:00+00:00" if approval == "approved" else None,
        "sessions": [],
        "talk_session_id": None,
    }
    (project_dir / ".kobito" / "tasks" / f"{task_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _write_order(project_dir: Path, order: list[str]):
    """task_order.json を書き込む"""
    (project_dir / ".kobito" / "task_order.json").write_text(
        json.dumps(order), encoding="utf-8"
    )


def _read_meta(project_dir: Path, task_id: str) -> dict:
    """タスクメタデータを読み込む"""
    meta_path = project_dir / ".kobito" / "tasks" / f"{task_id}.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


@pytest.fixture
def project_dir(tmp_path):
    return _setup_project(tmp_path)


@pytest.fixture
def mock_bridge():
    """CLIBridgeモック — run_stream は done イベントを返す async generator"""
    bridge = MagicMock()
    bridge.shutdown = AsyncMock()

    async def fake_stream(**kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "完了"}]}}
        yield {"type": "result", "session_id": "sess_auto_001"}

    bridge.run_stream = MagicMock(side_effect=fake_stream)
    return bridge


@pytest.fixture
def mock_config(project_dir):
    config = MagicMock()
    config.get_agent.return_value = AgentInfo(
        id="system", name="レプリカ", path=str(project_dir),
        cli="claude", model_tier="deep", system_prompt="",
    )
    config.list_agents.return_value = [config.get_agent.return_value]
    return config


# ================================================================
# Scheduler 単体テスト — 初期化
# ================================================================

class TestSchedulerInit:
    """スケジューラーの初期状態"""

    def test_起動時にOFF状態で初期化(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(
            config_manager=mock_config,
            cli_bridge=mock_bridge,
        )
        assert sched.enabled is False

    def test_起動時に実行中フラグがOFF(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(
            config_manager=mock_config,
            cli_bridge=mock_bridge,
        )
        assert sched.running is False

    def test_起動時にlast_runがNone(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(
            config_manager=mock_config,
            cli_bridge=mock_bridge,
        )
        assert sched.last_run is None

    def test_起動時にnext_runがNone(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(
            config_manager=mock_config,
            cli_bridge=mock_bridge,
        )
        assert sched.next_run is None


# ================================================================
# Scheduler 単体テスト — トグル
# ================================================================

class TestSchedulerToggle:
    """トグル操作"""

    def test_トグルでONになる(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        assert sched.enabled is True

    def test_トグル2回でOFFに戻る(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        sched.toggle()
        assert sched.enabled is False

    def test_ONにするとnext_runが設定される(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        assert sched.next_run is not None

    def test_OFFにするとnext_runがNoneに戻る(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()  # ON
        sched.toggle()  # OFF
        assert sched.next_run is None


# ================================================================
# Scheduler 単体テスト — tick（実行ロジック）
# ================================================================

class TestSchedulerTick:
    """tick() — 1回の発火サイクル"""

    @pytest.mark.asyncio
    async def test_OFF状態ではタスクが実行されない(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        # enabled=False のまま
        await sched.tick()
        mock_bridge.run_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_実行中フラグONの間は次の実行がスキップされる(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()  # ON
        sched.running = True  # 実行中フラグを手動で立てる
        await sched.tick()
        mock_bridge.run_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_order先頭から順に評価(self, project_dir, mock_bridge, mock_config):
        """先頭の承認済みタスクが実行対象になる"""
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _create_task(project_dir, "task_002", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001", "task_002"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()  # ON
        await sched.tick()

        # run_stream が呼ばれ、prompt に task_001 のコンテキストが含まれる
        mock_bridge.run_stream.assert_called_once()
        call_kwargs = mock_bridge.run_stream.call_args.kwargs
        assert "承認済みタスク" in call_kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_未承認タスクはスキップ(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_pending", TASK_PENDING, approval="pending")
        _create_task(project_dir, "task_approved", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_pending", "task_approved"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        # task_pending はスキップされ、task_approved が実行される
        mock_bridge.run_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_doneタスクはスキップ(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_done", TASK_APPROVED_DONE, approval="approved")
        _create_task(project_dir, "task_active", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_done", "task_active"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        mock_bridge.run_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_条件を満たすタスクがない場合何も実行しない(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_done", TASK_APPROVED_DONE, approval="approved")
        _create_task(project_dir, "task_pending", TASK_PENDING, approval="pending")
        _write_order(project_dir, ["task_done", "task_pending"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        mock_bridge.run_stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_空のtask_orderでも何も実行しない(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _write_order(project_dir, [])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        mock_bridge.run_stream.assert_not_called()


# ================================================================
# Scheduler 単体テスト — セッション管理
# ================================================================

class TestSchedulerSession:
    """セッション開始・完了"""

    @pytest.mark.asyncio
    async def test_対象タスク発見時に新規作業セッション開始(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        mock_bridge.run_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_セッション開始時にタスクコンテキストwork注入(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        call_kwargs = mock_bridge.run_stream.call_args.kwargs
        prompt = call_kwargs["prompt"]
        # task_mode: work のテンプレートが注入される
        assert "<task-context>" in prompt
        assert "未完了ステップを1つだけ実行" in prompt

    @pytest.mark.asyncio
    async def test_セッションIDがメタデータに追記される(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        meta = _read_meta(project_dir, "task_001")
        assert "sess_auto_001" in meta["sessions"]

    @pytest.mark.asyncio
    async def test_doneイベントで実行中フラグがOFFになる(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        # fake_stream は result イベント（= done）を返すので、完了後 running は False
        assert sched.running is False


# ================================================================
# Scheduler 単体テスト — 状態管理
# ================================================================

class TestSchedulerState:
    """last_run / next_run / タイマーループ"""

    @pytest.mark.asyncio
    async def test_last_runが実行ごとに更新される(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        before = datetime.now(timezone.utc)
        await sched.tick()

        assert sched.last_run is not None
        assert sched.last_run >= before

    @pytest.mark.asyncio
    async def test_next_runがON状態で正しく算出される(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        _create_task(project_dir, "task_001", TASK_APPROVED_DRAFT, approval="approved")
        _write_order(project_dir, ["task_001"])

        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()
        await sched.tick()

        # tick後もON状態なので next_run は last_run + 10分
        assert sched.next_run is not None
        assert sched.last_run is not None
        diff = (sched.next_run - sched.last_run).total_seconds()
        assert diff == pytest.approx(600, abs=1)  # 10分 = 600秒

    @pytest.mark.asyncio
    async def test_サーバー停止時にタイマーループが正常終了する(self, project_dir, mock_bridge, mock_config):
        from server.scheduler import Scheduler
        sched = Scheduler(config_manager=mock_config, cli_bridge=mock_bridge)
        sched.toggle()

        # ループ開始（即座にキャンセル）
        loop_task = asyncio.create_task(sched.run_loop())
        await asyncio.sleep(0.05)
        await sched.stop()

        # タスクが正常終了していること（CancelledError が外に漏れない）
        assert loop_task.done() or loop_task.cancelled()


# ================================================================
# Web API テスト
# ================================================================

@pytest.fixture
def scheduler_app(project_dir, mock_config, mock_bridge):
    """スケジューラーAPI付きのテストアプリ"""
    from server.app import create_app

    app = create_app(
        config_manager=mock_config,
        session_reader=MagicMock(),
        cli_bridge=mock_bridge,
    )
    return app


class TestSchedulerStatusAPI:
    """GET /api/scheduler/status"""

    def test_スケジューラー状態が返る(self, scheduler_app):
        with TestClient(scheduler_app) as client:
            resp = client.get("/api/scheduler/status")

        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "running" in data
        assert "last_run" in data
        assert "next_run" in data
        # 初期状態はOFF
        assert data["enabled"] is False
        assert data["running"] is False


class TestSchedulerToggleAPI:
    """POST /api/scheduler/toggle"""

    def test_トグルでONになる(self, scheduler_app):
        with TestClient(scheduler_app) as client:
            resp = client.post("/api/scheduler/toggle")

        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True

    def test_トグル2回でOFFに戻る(self, scheduler_app):
        with TestClient(scheduler_app) as client:
            client.post("/api/scheduler/toggle")
            resp = client.post("/api/scheduler/toggle")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
