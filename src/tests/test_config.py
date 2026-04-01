"""ConfigManagerのテスト"""

import json

import pytest


class TestConfigManagerInit:
    """起動時の自動登録"""

    def test_systemエージェントが自動登録される(self, tmp_data_dir, tmp_project_dir):
        """agents.jsonが存在しない場合、systemエージェントを含むファイルが作成される"""
        from server.config import ConfigManager

        agents_file = tmp_data_dir / "agents.json"
        assert not agents_file.exists()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agents = cm.list_agents()

        assert agents_file.exists()
        assert len(agents) == 1
        assert agents[0].id == "system"
        assert agents[0].name == "レプリカ"
        assert agents[0].path == str(tmp_project_dir)

    def test_既存のagents_jsonがあれば自動登録しない(self, tmp_data_dir, agents_json):
        """既にagents.jsonが存在する場合、上書きしない"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path="dummy")
        agents = cm.list_agents()

        assert len(agents) == 1
        assert agents[0].id == "system"


class TestConfigManagerListGet:
    """一覧・詳細取得"""

    def test_エージェント一覧が取得できる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agents = cm.list_agents()

        assert len(agents) == 1
        assert agents[0].id == "system"
        assert agents[0].cli == "claude"
        assert agents[0].model_tier == "deep"

    def test_エージェント詳細が取得できる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agent = cm.get_agent("system")

        assert agent.id == "system"
        assert agent.name == "レプリカ"
        assert agent.description == "システム管理エージェント"
        assert "テストプロジェクト" in agent.system_prompt

    def test_存在しないエージェントIDでエラー(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager, AgentNotFoundError

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(AgentNotFoundError):
            cm.get_agent("nonexistent")


class TestConfigManagerUpdate:
    """設定更新"""

    def test_name_descriptionを更新できる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        updated = cm.update_agent("system", name="新しい名前", description="新しい説明")

        assert updated.name == "新しい名前"
        assert updated.description == "新しい説明"

        # ファイルにも反映されている
        reloaded = cm.get_agent("system")
        assert reloaded.name == "新しい名前"

    def test_model_tierを更新できる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        updated = cm.update_agent("system", model_tier="quick")

        assert updated.model_tier == "quick"

        reloaded = cm.get_agent("system")
        assert reloaded.model_tier == "quick"


class TestConfigManagerSystemPrompt:
    """CLAUDE.mdの読み書き"""

    def test_CLAUDE_mdを読み取れる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        prompt = cm.get_system_prompt("system")

        assert "テストプロジェクト" in prompt

    def test_CLAUDE_mdを更新できる(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm.update_system_prompt("system", "# 更新されたプロンプト")

        # ファイルに反映されている
        content = (tmp_project_dir / "CLAUDE.md").read_text(encoding="utf-8")
        assert content == "# 更新されたプロンプト"

        # get_agentからも読める
        agent = cm.get_agent("system")
        assert "更新されたプロンプト" in agent.system_prompt

    def test_CLAUDE_mdが存在しないプロジェクト(self, tmp_data_dir, tmp_path):
        """CLAUDE.mdがないプロジェクトでもsystem_promptは空文字で返る"""
        from server.config import ConfigManager

        project_dir = tmp_path / "no_claude_md"
        project_dir.mkdir()

        agents = [
            {
                "id": "bare",
                "name": "Bare",
                "path": str(project_dir),
                "description": "",
                "cli": "claude",
                "model_tier": "quick",
            }
        ]
        (tmp_data_dir / "agents.json").write_text(json.dumps(agents, ensure_ascii=False), encoding="utf-8")

        cm = ConfigManager(data_dir=tmp_data_dir, system_path="dummy")
        agent = cm.get_agent("bare")

        assert agent.system_prompt == ""
