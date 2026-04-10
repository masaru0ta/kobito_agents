"""ダッシュボード E2E テスト（Playwright）"""

import json
import threading
import time

import pytest
import uvicorn

from server.app import create_app
from server.config import ConfigManager
from server.session_reader import ClaudeSessionReader
from server.cli_bridge import CLIBridge


@pytest.fixture(scope="module")
def test_env(tmp_path_factory):
    """テスト用環境を構築しサーバーを起動する"""
    base = tmp_path_factory.mktemp("e2e_dashboard")

    # エージェントのワーキングディレクトリ
    project_dir = base / "test_project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# テストプロジェクト", encoding="utf-8")

    # .kobito/dashboard.md を事前に配置
    kobito_dir = project_dir / ".kobito"
    kobito_dir.mkdir()
    (kobito_dir / "dashboard.md").write_text(
        "# coder\n\nこれはダッシュボードの**テスト**内容。\n\n[仕様書](docs/spec.md)",
        encoding="utf-8",
    )

    # data/agents.json
    data_dir = base / "data"
    data_dir.mkdir()
    agents = [{
        "id": "coder",
        "name": "coder",
        "path": str(project_dir),
        "description": "実装担当",
        "cli": "claude",
        "model_tier": "standard",
    }]
    (data_dir / "agents.json").write_text(json.dumps(agents, ensure_ascii=False), encoding="utf-8")

    # サーバー起動
    config = ConfigManager(data_dir=data_dir, system_path=str(project_dir))
    reader = ClaudeSessionReader(claude_home=base / ".claude")
    bridge = CLIBridge()
    app = create_app(config_manager=config, session_reader=reader, cli_bridge=bridge)

    port = 18400
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/agents").status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.1)

    yield {
        "url": f"http://127.0.0.1:{port}",
        "project_dir": project_dir,
    }

    server.should_exit = True


class TestDashboardTab:
    """ダッシュボードタブの表示"""

    def test_ダッシュボードタブが先頭に表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".tab")

        tabs = page.query_selector_all(".tab")
        assert len(tabs) >= 1
        assert "ダッシュボード" in tabs[0].text_content()

    def test_ダッシュボードタブがデフォルトで選択される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".tab.active")

        active_tab = page.query_selector(".tab.active")
        assert "ダッシュボード" in active_tab.text_content()


class TestDashboardView:
    """ダッシュボード表示モード"""

    def test_dashboard_md_がレンダリングされる(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-body")

        # h1 タグがレンダリングされている
        h1 = page.query_selector(".dashboard-body h1")
        assert h1 is not None
        assert "coder" in h1.text_content()

    def test_マークダウンのboldがレンダリングされる(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-body")

        strong = page.query_selector(".dashboard-body strong")
        assert strong is not None
        assert "テスト" in strong.text_content()

    def test_編集ボタンが表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-edit-btn")

        btn = page.query_selector(".dashboard-edit-btn")
        assert btn is not None


class TestDashboardEdit:
    """ダッシュボード編集モード"""

    def test_編集ボタンを押すとテキストエリアが開く(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-edit-btn")
        page.click(".dashboard-edit-btn")

        editor = page.query_selector(".dashboard-editor")
        assert editor is not None
        assert editor.is_visible()

    def test_テキストエリアにdashboard_mdの内容が入る(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-edit-btn")
        page.click(".dashboard-edit-btn")
        page.wait_for_selector(".dashboard-editor")

        content = page.query_selector(".dashboard-editor").input_value()
        assert "# coder" in content

    def test_保存するとダッシュボードに反映される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-edit-btn")
        page.click(".dashboard-edit-btn")
        page.wait_for_selector(".dashboard-editor")

        page.fill(".dashboard-editor", "# 更新済み\n\n保存テスト")
        page.click(".dashboard-save-btn")
        page.wait_for_selector(".dashboard-body")

        h1 = page.query_selector(".dashboard-body h1")
        assert "更新済み" in h1.text_content()

    def test_キャンセルすると変更が破棄される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-edit-btn")
        page.click(".dashboard-edit-btn")
        page.wait_for_selector(".dashboard-editor")

        page.fill(".dashboard-editor", "# 破棄される内容")
        page.click(".dashboard-cancel-btn")
        page.wait_for_selector(".dashboard-body")

        body_text = page.query_selector(".dashboard-body").text_content()
        assert "破棄される内容" not in body_text


class TestDashboardFileLink:
    """ファイルリンクの動作"""

    def test_ファイルリンクをクリックするとファイルタブに遷移する(self, test_env, page):
        # 前のテストでファイルが書き換えられる可能性があるため、リンク付きの内容に戻す
        (test_env["project_dir"] / ".kobito" / "dashboard.md").write_text(
            "# coder\n\nこれはダッシュボードの**テスト**内容。\n\n[仕様書](docs/spec.md)",
            encoding="utf-8",
        )
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")
        page.click(".agent-item")
        page.wait_for_selector(".dashboard-body a")

        page.click(".dashboard-body a")

        # ファイルタブがアクティブになる
        page.wait_for_selector(".tab.active")
        active_tab = page.query_selector(".tab.active")
        assert "ファイル" in active_tab.text_content()
