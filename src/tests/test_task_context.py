"""タスクコンテキスト注入のテスト"""

from pathlib import Path

import pytest

from server.task_manager import Task


# ================================================================
# ヘルパー
# ================================================================

def _make_task(**overrides) -> Task:
    defaults = dict(
        task_id="task_20260401_170000_abc",
        title="テストタスク",
        agent="system",
        phase="draft",
        created="2026-04-01T17:00:00Z",
        approval="pending",
        body="## 作業ステップ\n\n- [ ] ステップ1\n- [x] ステップ2",
    )
    defaults.update(overrides)
    return Task(**defaults)


# ================================================================
# build_task_context 単体テスト
# ================================================================

class TestBuildTaskContext:
    """build_task_context がテンプレートを正しく展開する"""

    def test_work_mode_contains_task_title(self):
        from server.task_context import build_task_context
        task = _make_task(title="速度改善")
        result = build_task_context(task, "work")
        assert "速度改善" in result

    def test_work_mode_contains_phase(self):
        from server.task_context import build_task_context
        task = _make_task(phase="doing")
        result = build_task_context(task, "work")
        assert "doing" in result

    def test_work_mode_contains_approval(self):
        from server.task_context import build_task_context
        task = _make_task(approval="approved")
        result = build_task_context(task, "work")
        assert "approved" in result

    def test_work_mode_contains_body(self):
        from server.task_context import build_task_context
        task = _make_task(body="## 背景\n\nパフォーマンス問題。")
        result = build_task_context(task, "work")
        assert "パフォーマンス問題。" in result

    def test_work_mode_contains_instruction(self):
        from server.task_context import build_task_context
        task = _make_task()
        result = build_task_context(task, "work")
        assert "未完了ステップを1つだけ実行" in result

    def test_talk_mode_contains_task_title(self):
        from server.task_context import build_task_context
        task = _make_task(title="設計相談")
        result = build_task_context(task, "talk")
        assert "設計相談" in result

    def test_talk_mode_contains_constraint(self):
        from server.task_context import build_task_context
        task = _make_task()
        result = build_task_context(task, "talk")
        assert "コードの変更やファイル操作は行わない" in result

    def test_talk_mode_no_work_instruction(self):
        """相談モードに作業指示が含まれないこと"""
        from server.task_context import build_task_context
        task = _make_task()
        result = build_task_context(task, "talk")
        assert "未完了ステップを1つだけ実行" not in result

    def test_work_mode_no_talk_constraint(self):
        """作業モードに相談制約が含まれないこと"""
        from server.task_context import build_task_context
        task = _make_task()
        result = build_task_context(task, "work")
        assert "コードの変更やファイル操作は行わない" not in result

    def test_invalid_mode_raises(self):
        from server.task_context import build_task_context
        task = _make_task()
        with pytest.raises(FileNotFoundError):
            build_task_context(task, "invalid")

    def test_output_wrapped_in_task_context_tag(self):
        from server.task_context import build_task_context
        task = _make_task()
        result = build_task_context(task, "work")
        assert result.startswith("<task-context>")
        assert "</task-context>" in result


# ================================================================
# API統合テスト — task_id 付きチャット
# ================================================================

class TestChatWithTaskContext:
    """POST /chat に task_id を渡した場合のコンテキスト注入"""

    def test_chat_with_task_id_injects_context(self, task_chat_app):
        """task_id 指定時、プロンプトにタスクコンテキストが付加される"""
        client, mock_bridge, task_id = task_chat_app
        resp = client.post(f"/api/agents/system/chat", json={
            "message": "作業開始",
            "task_id": task_id,
            "task_mode": "work",
        })
        assert resp.status_code == 200
        # run_stream に渡された prompt を検証
        call_kwargs = mock_bridge.run_stream.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[1]
        assert "テストタスク" in prompt
        assert "作業開始" in prompt

    def test_chat_with_task_id_talk_mode(self, task_chat_app):
        """task_mode=talk で相談用コンテキストが注入される"""
        client, mock_bridge, task_id = task_chat_app
        resp = client.post(f"/api/agents/system/chat", json={
            "message": "相談したい",
            "task_id": task_id,
            "task_mode": "talk",
        })
        assert resp.status_code == 200
        call_kwargs = mock_bridge.run_stream.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[1]
        assert "コードの変更やファイル操作は行わない" in prompt

    def test_chat_without_task_id_no_injection(self, task_chat_app):
        """task_id なしの通常チャットではコンテキスト注入されない"""
        client, mock_bridge, _ = task_chat_app
        resp = client.post(f"/api/agents/system/chat", json={
            "message": "こんにちは",
        })
        assert resp.status_code == 200
        call_kwargs = mock_bridge.run_stream.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[1]
        assert "<task-context>" not in prompt

    def test_chat_with_nonexistent_task_returns_404(self, task_chat_app):
        """存在しないtask_idで404が返る"""
        client, _, _ = task_chat_app
        resp = client.post(f"/api/agents/system/chat", json={
            "message": "作業開始",
            "task_id": "task_99990101_000000_zzz",
            "task_mode": "work",
        })
        assert resp.status_code == 404

    def test_chat_task_mode_defaults_to_work(self, task_chat_app):
        """task_mode 省略時は work がデフォルト"""
        client, mock_bridge, task_id = task_chat_app
        resp = client.post(f"/api/agents/system/chat", json={
            "message": "作業開始",
            "task_id": task_id,
        })
        assert resp.status_code == 200
        call_kwargs = mock_bridge.run_stream.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs.args[1]
        assert "未完了ステップを1つだけ実行" in prompt


# ================================================================
# fixture
# ================================================================

TASK_MD = """\
---
title: テストタスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
---

## 作業ステップ

- [ ] ステップ1
- [x] ステップ2
"""


@pytest.fixture
def task_chat_app(tmp_path):
    """タスク付きチャットAPI用のテストアプリ"""
    from unittest.mock import AsyncMock, MagicMock
    from server.app import create_app
    from server.config import AgentInfo
    from fastapi.testclient import TestClient

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# test", encoding="utf-8")

    # タスクファイル作成
    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir()
    task_id = "task_20260401_170000_abc"
    (tasks_dir / f"{task_id}.md").write_text(TASK_MD, encoding="utf-8")

    # .kobitoディレクトリ
    meta_dir = project_dir / ".kobito" / "tasks"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".kobito" / "meta").mkdir(parents=True, exist_ok=True)

    mock_config = MagicMock()
    mock_config.get_agent.return_value = AgentInfo(
        id="system", name="レプリカ", path=str(project_dir),
        cli="claude", model_tier="deep", system_prompt="",
    )

    mock_reader = MagicMock()

    # run_stream を async generator として振る舞わせる
    async def fake_stream(**kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}
        yield {"type": "result", "session_id": "sess_001"}

    mock_bridge = MagicMock()
    mock_bridge.run_stream = MagicMock(side_effect=fake_stream)
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )
    client = TestClient(app)

    return client, mock_bridge, task_id
