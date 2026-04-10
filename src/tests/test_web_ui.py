"""Web UI e2eテスト（Playwright）"""

import json
import threading
import time

import pytest
import uvicorn

from server.app import create_app
from server.config import ConfigManager
from server.session_reader import AgentSessionReader
from server.cli_bridge import CLIBridge
from tests.conftest import make_session_jsonl


def _user_msg(uuid, content, timestamp, session_id="sess-001", parent_uuid=None):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {"role": "user", "content": content},
        "timestamp": timestamp,
        "sessionId": session_id,
    }


def _assistant_msg(uuid, content, timestamp, session_id="sess-001", parent_uuid=None, tool_uses=None):
    msg_content = [{"type": "text", "text": content}]
    if tool_uses:
        for tu in tool_uses:
            msg_content.append({"type": "tool_use", "name": tu["name"], "input": tu.get("input", {})})
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {"role": "assistant", "content": msg_content, "model": "claude-opus-4-6"},
        "timestamp": timestamp,
        "sessionId": session_id,
    }


@pytest.fixture(scope="module")
def test_env(tmp_path_factory):
    """テスト用環境を構築しサーバーを起動する"""
    base = tmp_path_factory.mktemp("e2e")

    # プロジェクトディレクトリ
    project_dir = base / "test_project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# テストプロジェクト\nテスト用のシステムプロンプト", encoding="utf-8")

    # data/agents.json
    data_dir = base / "data"
    data_dir.mkdir()
    agents = [{
        "id": "system",
        "name": "レプリカ",
        "path": str(project_dir),
        "description": "システム管理エージェント",
        "cli": "claude",
        "model_tier": "deep",
    }]
    (data_dir / "agents.json").write_text(json.dumps(agents, ensure_ascii=False), encoding="utf-8")

    # Claude Codeセッションデータを模擬
    project_hash = str(project_dir).replace("\\", "-").replace(":", "-").replace("/", "-").replace("_", "-")
    sessions_dir = base / ".claude" / "projects" / project_hash
    sessions_dir.mkdir(parents=True)

    make_session_jsonl(sessions_dir, "sess-001", [
        _user_msg("u1", "こんにちは", "2026-04-01T06:00:00Z"),
        _assistant_msg("a1", "やあ、レプリカだ。", "2026-04-01T06:00:05Z", parent_uuid="u1"),
        _user_msg("u2", "仕様書を見て", "2026-04-01T06:01:00Z", parent_uuid="a1"),
        _assistant_msg("a2", "確認した。問題ない。", "2026-04-01T06:01:10Z", parent_uuid="u2",
                       tool_uses=[{"name": "Read", "input": {"file_path": "spec.md"}}]),
    ])

    make_session_jsonl(sessions_dir, "sess-002", [
        _user_msg("u3", "設計を始めよう", "2026-04-01T07:00:00Z", session_id="sess-002"),
        _assistant_msg("a3", "了解した。", "2026-04-01T07:00:05Z", session_id="sess-002", parent_uuid="u3"),
    ])

    # sess-003: Markdown表示と空メッセージのテスト用
    make_session_jsonl(sessions_dir, "sess-003", [
        _user_msg("u4", "Markdownテスト", "2026-04-01T08:00:00Z", session_id="sess-003"),
        _assistant_msg("a4", "**太字**と`コード`と\n- リスト1\n- リスト2", "2026-04-01T08:00:05Z", session_id="sess-003", parent_uuid="u4"),
        _user_msg("u5", "", "2026-04-01T08:00:10Z", session_id="sess-003", parent_uuid="a4"),  # 空メッセージ
        _assistant_msg("a5", "", "2026-04-01T08:00:15Z", session_id="sess-003", parent_uuid="u5"),  # 空レスポンス
        _user_msg("u6", "次の質問", "2026-04-01T08:01:00Z", session_id="sess-003", parent_uuid="a5"),
        _assistant_msg("a6", "回答だ。", "2026-04-01T08:01:05Z", session_id="sess-003", parent_uuid="u6"),
    ])

    # サーバー起動
    config = ConfigManager(data_dir=data_dir, system_path=str(project_dir))
    reader = AgentSessionReader(claude_home=base / ".claude")
    bridge = CLIBridge()
    app = create_app(config_manager=config, session_reader=reader, cli_bridge=bridge)

    port = 18300
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # サーバー起動待ち
    import httpx
    for _ in range(50):
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/api/agents")
            if resp.status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.1)

    yield {
        "url": f"http://127.0.0.1:{port}",
        "project_dir": project_dir,
        "data_dir": data_dir,
    }

    server.should_exit = True


class TestSidebar:
    """左ペイン: エージェント一覧"""

    def test_エージェント一覧が表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".agent-item")

        agents = page.query_selector_all(".agent-item")
        assert len(agents) >= 1

        # システムエージェントの名前が表示されている
        text = page.text_content(".agent-list")
        assert "レプリカ" in text


def _switch_to_chat_tab(page, url: str):
    """エージェント自動選択後にセッションタブに切り替える"""
    page.goto(url)
    page.wait_for_selector(".agent-item")
    # ダッシュボードタブがアクティブになるまで待つ（アプリ完全初期化の指標）
    page.wait_for_selector(".tab[data-tab='dashboard'].active")
    page.click(".tab[data-tab='chat']")
    # チャットタブがアクティブになるまで待つ（タブ切り替え完了の指標）
    page.wait_for_selector(".tab[data-tab='chat'].active")


class TestSessionList:
    """中央ペイン: セッションリスト"""

    def test_セッション一覧が表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")

        items = page.query_selector_all(".conversation-item")
        assert len(items) == 3

    def test_セッション一覧に件数が表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conv-count")

        counts = page.query_selector_all(".conv-count")
        texts = [c.text_content() for c in counts]
        # sess-001は4件、sess-002は2件
        assert "(4)" in texts or "(2)" in texts

    def test_セッション一覧にプレビューが表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conv-preview")

        text = page.text_content(".conversation-list")
        assert "確認した" in text or "了解した" in text


class TestChatPane:
    """右ペイン: チャット画面"""

    def test_セッション選択でチャット履歴が表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")

        # 最初のセッションをクリック
        page.query_selector(".conversation-item").click()
        page.wait_for_selector(".message")

        messages = page.query_selector_all(".message")
        assert len(messages) >= 2

    def test_ユーザーメッセージが右寄せで表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        page.query_selector(".conversation-item").click()
        page.wait_for_selector(".message.user")

        user_msg = page.query_selector(".message.user")
        assert user_msg is not None

    def test_ツール使用通知が表示される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        # sess-001（tool_use付き）を選択。一覧は新しい順なのでsess-002が先、sess-001が2番目
        items = page.query_selector_all(".conversation-item")
        items[-1].click()  # 古い方 = sess-001
        page.wait_for_selector(".message")
        page.wait_for_selector(".tool-use-notice", timeout=5000)

        tool = page.query_selector(".tool-use-notice")
        assert tool is not None
        assert "Read" in tool.text_content()

    def test_新規会話ボタンが存在する(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".new-chat-btn")

        btn = page.query_selector(".new-chat-btn")
        assert btn is not None


class TestSettingsTab:
    """設定タブ"""

    def test_設定タブに切り替えできる(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".tab[data-tab='settings']")

        page.query_selector(".tab[data-tab='settings']").click()
        page.wait_for_selector("#settings-tab-content.visible, .settings-content.visible")

    def test_名前が表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".tab[data-tab='settings']")

        page.query_selector(".tab[data-tab='settings']").click()
        page.wait_for_selector("input[data-field='name']")
        name_input = page.query_selector("input[data-field='name']")
        assert name_input.input_value() == "レプリカ"

    def test_CLAUDE_mdが編集エリアに表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".tab[data-tab='settings']")

        page.query_selector(".tab[data-tab='settings']").click()
        page.wait_for_selector("textarea[data-field='system-prompt']")
        textarea = page.query_selector("textarea[data-field='system-prompt']")
        assert "テストプロジェクト" in textarea.input_value()

    def test_model_tier選択が表示される(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".tab[data-tab='settings']")

        page.query_selector(".tab[data-tab='settings']").click()
        page.wait_for_selector("select[data-field='model_tier']")
        select = page.query_selector("select[data-field='model_tier']")
        assert select is not None


class TestMessageRendering:
    """メッセージ表示の品質"""

    def test_空メッセージはスキップされる(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        # sess-003を選択（空メッセージを含む）
        items = page.query_selector_all(".conversation-item")
        for item in items:
            if "回答だ" in (item.text_content() or ""):
                item.click()
                break
        page.wait_for_selector(".message")

        # 空バブルがないことを確認（全バブルにテキストがある）
        bubbles = page.query_selector_all(".message-bubble")
        for bubble in bubbles:
            text = bubble.text_content().strip()
            assert len(text) > 0, "空のメッセージバブルが表示されている"

    def test_Markdownがレンダリングされる(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        items = page.query_selector_all(".conversation-item")
        for item in items:
            if "回答だ" in (item.text_content() or ""):
                item.click()
                break
        page.wait_for_selector(".message-bubble strong", timeout=5000)

        strong = page.query_selector(".message-bubble strong")
        assert strong is not None, "Markdownの太字がレンダリングされていない"

    def test_改行が反映される(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        items = page.query_selector_all(".conversation-item")
        for item in items:
            if "回答だ" in (item.text_content() or ""):
                item.click()
                break
        page.wait_for_selector(".message-bubble li", timeout=5000)

        li = page.query_selector(".message-bubble li")
        assert li is not None, "Markdownのリストがレンダリングされていない"


class TestResize:
    """リサイズ機能"""

    def test_リサイズハンドルが存在する(self, test_env, page):
        page.goto(test_env["url"])
        page.wait_for_selector(".resize-handle")

        handle = page.query_selector(".resize-handle")
        assert handle is not None


class TestChatActions:
    """チャットヘッダーのアクション（メニュー内）"""

    def test_非表示ボタンが存在する(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        page.query_selector(".conversation-item").click()
        page.wait_for_selector("#chat-menu-btn")

        # メニューを開く
        page.query_selector("#chat-menu-btn").click()
        page.wait_for_selector("#btn-hide")

        btn = page.query_selector("#btn-hide")
        assert btn is not None
        assert "非表示" in btn.text_content()

    def test_CLI起動ボタンが存在する(self, test_env, page):
        _switch_to_chat_tab(page, test_env["url"])
        page.wait_for_selector(".conversation-item")
        page.query_selector(".conversation-item").click()
        page.wait_for_selector("#chat-menu-btn")

        # メニューを開く
        page.query_selector("#chat-menu-btn").click()
        page.wait_for_selector("#btn-cli")

        btn = page.query_selector("#btn-cli")
        assert btn is not None
        assert "CLI" in btn.text_content()
