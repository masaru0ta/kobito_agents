"""ConfigManagerのテスト"""

import json
import re

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


class TestConfigManagerAddAgent:
    """エージェント追加"""

    def test_エージェントを追加できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agent = cm.add_agent(
            name="キャスパー",
            path=str(new_project),
            description="ゲームデザイナー",
            cli="claude",
            model_tier="deep",
        )

        assert agent.name == "キャスパー"
        assert agent.path == str(new_project)
        assert agent.description == "ゲームデザイナー"
        assert agent.cli == "claude"
        assert agent.model_tier == "deep"

    def test_追加したエージェントのIDがタイムスタンプ形式(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agent = cm.add_agent(
            name="テスト",
            path=str(new_project),
            description="",
            cli="claude",
            model_tier="deep",
        )

        # agent_{YYYYMMDDHHmmss}_{ランダム3文字} 形式
        assert re.match(r"^agent_\d{8}_\d{6}_[a-z0-9]{3}$", agent.id)

    def test_追加したエージェントがagents_jsonに永続化される(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm.add_agent(
            name="テスト",
            path=str(new_project),
            description="",
            cli="claude",
            model_tier="deep",
        )

        agents = cm.list_agents()
        assert len(agents) == 2
        assert agents[1].name == "テスト"

    def test_nameが空ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="name"):
            cm.add_agent(name="", path=str(new_project), description="", cli="claude", model_tier="deep")

    def test_pathが空ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="path"):
            cm.add_agent(name="テスト", path="", description="", cli="claude", model_tier="deep")

    def test_pathが存在しないディレクトリならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="path"):
            cm.add_agent(
                name="テスト",
                path=str(tmp_path / "nonexistent"),
                description="",
                cli="claude",
                model_tier="deep",
            )

    def test_cliが不正値ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="cli"):
            cm.add_agent(name="テスト", path=str(new_project), description="", cli="invalid", model_tier="deep")

    def test_model_tierが不正値ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="model_tier"):
            cm.add_agent(name="テスト", path=str(new_project), description="", cli="claude", model_tier="invalid")

    def test_同一pathのエージェントが既に登録済みならエラー(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager, DuplicatePathError

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        # tmp_project_dir は既に system エージェントとして登録済み
        with pytest.raises(DuplicatePathError):
            cm.add_agent(
                name="重複",
                path=str(tmp_project_dir),
                description="",
                cli="claude",
                model_tier="deep",
            )


class TestConfigManagerDeleteAgent:
    """エージェント削除"""

    def test_エージェントを削除できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        # まず追加してから削除する
        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agent = cm.add_agent(
            name="削除対象",
            path=str(new_project),
            description="",
            cli="claude",
            model_tier="deep",
        )

        cm.delete_agent(agent.id)

        agents = cm.list_agents()
        assert len(agents) == 1
        assert agents[0].id == "system"

    def test_systemエージェントは削除できない(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager, SystemAgentProtectedError

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(SystemAgentProtectedError):
            cm.delete_agent("system")

    def test_存在しないエージェントの削除でエラー(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager, AgentNotFoundError

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(AgentNotFoundError):
            cm.delete_agent("nonexistent")

    def test_削除後もagents_jsonに永続化される(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        new_project = tmp_path / "new_project"
        new_project.mkdir()

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agent = cm.add_agent(
            name="削除対象",
            path=str(new_project),
            description="",
            cli="claude",
            model_tier="deep",
        )

        cm.delete_agent(agent.id)

        # 新しいConfigManagerインスタンスで読み直しても反映されている
        cm2 = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agents = cm2.list_agents()
        assert len(agents) == 1
        assert agents[0].id == "system"


class TestConfigManagerPhase7Settings:
    """Phase 7 追加設定: lmstudio_url / team_max_turns"""

    def test_lmstudio_urlのデフォルト値が返る(self, tmp_data_dir, agents_json, tmp_project_dir):
        """settings.jsonに lmstudio_url がない場合、デフォルト 'http://localhost:1234/v1' を返す"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        assert cm.get_setting("lmstudio_url") == "http://localhost:1234/v1"

    def test_team_max_turnsのデフォルト値が返る(self, tmp_data_dir, agents_json, tmp_project_dir):
        """settings.jsonに team_max_turns がない場合、デフォルト 20 を返す"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        assert cm.get_setting("team_max_turns") == 20

    def test_lmstudio_urlを書き込んで読み直せる(self, tmp_data_dir, agents_json, tmp_project_dir):
        """set_setting で lmstudio_url を更新した後、get_setting で取得できる"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm.set_setting("lmstudio_url", "http://192.168.1.10:1234/v1")
        assert cm.get_setting("lmstudio_url") == "http://192.168.1.10:1234/v1"

    def test_team_max_turnsを書き込んで読み直せる(self, tmp_data_dir, agents_json, tmp_project_dir):
        """set_setting で team_max_turns を更新した後、get_setting で取得できる"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm.set_setting("team_max_turns", 5)
        assert cm.get_setting("team_max_turns") == 5

    def test_settings_jsonに永続化される(self, tmp_data_dir, agents_json, tmp_project_dir):
        """書き込んだ設定が settings.json ファイルに正しく保存される"""
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm.set_setting("lmstudio_url", "http://localhost:5678/v1")
        cm.set_setting("team_max_turns", 10)

        # ファイルを直接読んで確認
        data = json.loads((tmp_data_dir / "settings.json").read_text(encoding="utf-8"))
        assert data["lmstudio_url"] == "http://localhost:5678/v1"
        assert data["team_max_turns"] == 10

    def test_別インスタンスからも読み直せる(self, tmp_data_dir, agents_json, tmp_project_dir):
        """書き込んだ設定が新しい ConfigManager インスタンスからも取得できる"""
        from server.config import ConfigManager

        cm1 = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        cm1.set_setting("lmstudio_url", "http://remotehost:1234/v1")

        cm2 = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        assert cm2.get_setting("lmstudio_url") == "http://remotehost:1234/v1"


class TestAgentInfoTeamModel:
    """AgentInfo のデータモデル拡張: type / members フィールド"""

    def test_typeのデフォルトはagent(self):
        from server.config import AgentInfo

        agent = AgentInfo(id="a1", name="テスト", path="/tmp")
        assert agent.type == "agent"

    def test_membersのデフォルトは空リスト(self):
        from server.config import AgentInfo

        agent = AgentInfo(id="a1", name="テスト", path="/tmp")
        assert agent.members == []

    def test_type_teamとmembersを指定できる(self):
        from server.config import AgentInfo

        team = AgentInfo(
            id="team_001",
            name="チームA",
            path="",
            type="team",
            members=["agent_001", "agent_002"],
        )
        assert team.type == "team"
        assert team.members == ["agent_001", "agent_002"]

    def test_pathは空文字でも許容される(self):
        """チームエージェントは作業ディレクトリを持たない"""
        from server.config import AgentInfo

        team = AgentInfo(id="team_001", name="チームA", path="", type="team", members=["a1"])
        assert team.path == ""


class TestConfigManagerTeamAgent:
    """チームエージェントの登録・取得・削除"""

    def test_チームを追加できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )

        team = cm.add_team(name="テストチーム", description="テスト用", members=[member.id])

        assert team.name == "テストチーム"
        assert team.type == "team"
        assert member.id in team.members

    def test_チームIDがteam_タイムスタンプ形式(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        import re
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )

        team = cm.add_team(name="チーム", description="", members=[member.id])

        assert re.match(r"^team_\d{8}_\d{6}_[a-z0-9]{3}$", team.id)

    def test_チームをagents_jsonに永続化できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )
        cm.add_team(name="チーム", description="", members=[member.id])

        # 別インスタンスで読み直す
        cm2 = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        agents = cm2.list_agents()
        team = next((a for a in agents if a.type == "team"), None)
        assert team is not None
        assert team.name == "チーム"

    def test_list_agentsにチームが含まれる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )
        cm.add_team(name="チーム", description="", members=[member.id])

        agents = cm.list_agents()
        types = [a.type for a in agents]
        assert "team" in types

    def test_get_agentでチームを取得できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )
        team = cm.add_team(name="チーム", description="説明", members=[member.id])

        fetched = cm.get_agent(team.id)
        assert fetched.id == team.id
        assert fetched.type == "team"
        assert fetched.members == [member.id]

    def test_membersが空ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir):
        from server.config import ConfigManager

        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))

        with pytest.raises(ValueError, match="members"):
            cm.add_team(name="空チーム", description="", members=[])

    def test_nameが空ならバリデーションエラー(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )

        with pytest.raises(ValueError, match="name"):
            cm.add_team(name="", description="", members=[member.id])

    def test_チームを削除できる(self, tmp_data_dir, agents_json, tmp_project_dir, tmp_path):
        from server.config import ConfigManager

        member_project = tmp_path / "member"
        member_project.mkdir()
        cm = ConfigManager(data_dir=tmp_data_dir, system_path=str(tmp_project_dir))
        member = cm.add_agent(
            name="メンバーA", path=str(member_project),
            description="", cli="claude", model_tier="quick",
        )
        team = cm.add_team(name="チーム", description="", members=[member.id])

        cm.delete_agent(team.id)

        agents = cm.list_agents()
        assert all(a.id != team.id for a in agents)
