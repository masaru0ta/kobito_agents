"""テスト共通フィクスチャ"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path):
    """一時的なdataディレクトリを返す"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def tmp_project_dir(tmp_path):
    """一時的なプロジェクトディレクトリを返す（CLAUDE.md付き）"""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# テストプロジェクト\nテスト用のシステムプロンプト", encoding="utf-8")
    return project_dir


@pytest.fixture
def agents_json(tmp_data_dir, tmp_project_dir):
    """systemエージェントが登録済みのagents.jsonを返す"""
    agents = [
        {
            "id": "system",
            "name": "レプリカ",
            "path": str(tmp_project_dir),
            "description": "システム管理エージェント",
            "cli": "claude",
            "model_tier": "deep",
        }
    ]
    agents_file = tmp_data_dir / "agents.json"
    agents_file.write_text(json.dumps(agents, ensure_ascii=False), encoding="utf-8")
    return agents_file


@pytest.fixture
def claude_sessions_dir(tmp_path, tmp_project_dir):
    """Claude Codeのセッションデータを模擬するディレクトリを返す"""
    # project_hashを生成（パスの \ と : を - に置換）
    project_path = str(tmp_project_dir).replace("\\", "-").replace(":", "-").replace("/", "-").replace("_", "-")
    sessions_dir = tmp_path / ".claude" / "projects" / project_path
    sessions_dir.mkdir(parents=True)
    return sessions_dir


def make_session_jsonl(sessions_dir: Path, session_id: str, messages: list[dict]) -> Path:
    """テスト用のセッションJSONLファイルを作成するヘルパー"""
    path = sessions_dir / f"{session_id}.jsonl"
    lines = []
    for msg in messages:
        lines.append(json.dumps(msg, ensure_ascii=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
