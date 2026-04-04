"""app.pyのテスト — デフォルトパス解決とアプリ起動"""

from unittest.mock import patch



class TestCreateAppDefaults:
    """create_appの引数なし呼び出し"""

    def test_引数なしでアプリが生成される(self, tmp_path):
        from server.app import create_app

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        project_root = tmp_path

        with patch("server.app.resolve_project_root", return_value=project_root):
            app = create_app()

        assert app.state.config_manager is not None
        assert app.state.session_reader is not None
        assert app.state.cli_bridge is not None

    def test_data_dirが自動作成される(self, tmp_path):
        from server.app import create_app

        project_root = tmp_path
        # data/ はまだ存在しない

        with patch("server.app.resolve_project_root", return_value=project_root):
            app = create_app()

        assert (project_root / "data").exists()

    def test_systemエージェントのpathがプロジェクトルートになる(self, tmp_path):
        from server.app import create_app

        project_root = tmp_path

        with patch("server.app.resolve_project_root", return_value=project_root):
            app = create_app()

        agents = app.state.config_manager.list_agents()
        assert len(agents) == 1
        assert agents[0].id == "system"
        assert agents[0].path == str(project_root)


class TestAppModuleLevel:
    """モジュールレベルのapp変数（uvicornから参照）"""

    def test_appモジュールにapp変数が存在する(self):
        from server import app as app_module

        assert hasattr(app_module, "app")


class TestResolveProjectRoot:
    """プロジェクトルートの解決"""

    def test_srcの親がプロジェクトルート(self):
        from server.app import resolve_project_root

        root = resolve_project_root()
        # server/app.py → server → src → プロジェクトルート
        assert (root / "CLAUDE.md").exists() or (root / "docs").exists()
