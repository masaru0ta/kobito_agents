"""ロギング設定 — フォーマッター・フィルター・初期化"""

from __future__ import annotations

import logging
import re

# ============================================================
# ANSI カラーコード
# ============================================================
_C = {
    "reset":   "\033[0m",
    "dim":     "\033[2m",
    "cyan":    "\033[36m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "red":     "\033[31m",
    "bold":    "\033[1m",
    "magenta": "\033[35m",
}

_LEVEL_COLOR = {
    logging.DEBUG:    _C["dim"],
    logging.INFO:     "",
    logging.WARNING:  _C["yellow"],
    logging.ERROR:    _C["red"],
    logging.CRITICAL: _C["bold"] + _C["red"],
}

# モジュール名ごとの強調色
_MODULE_COLOR = {
    "server.cli_bridge": _C["cyan"],
    "server.scheduler":  _C["green"],
}

# モジュール名を出力しないロガー（メッセージ自体で文脈が明らか）
_NO_MODULE_NAME = {"server.cli_bridge", "server.routes.chat"}

# cli_bridge プロセスイベントのキーワード（メッセージ先頭マッチ）
_PROCESS_KW_RE = re.compile(
    r"^(プロセス起動"
    r"|アイドルタイムアウトによりプロセス終了"
    r"|プロセス異常終了を検出"
    r"|モデル変更検出[^:]*)"
)

# アクセスログ用
_ACCESS_EXTRACT = re.compile(r'"((?:GET|POST|PUT|DELETE|PATCH|HEAD) \S+)[^"]*" (\d{3})')
_STATUS_COLOR   = {"2": _C["dim"], "4": _C["yellow"], "5": _C["red"]}


# ============================================================
# フォーマッター
# ============================================================

class ColoredFormatter(logging.Formatter):
    """通常ログ用カラーフォーマッター。

    chat.py 側が extra= で構造化データを渡した場合は専用レイアウトで出力する。
    それ以外はモジュール名・ログレベルに応じた色付きフォーマットで出力する。
    """

    def _fmt_chat(self, ts: str, record: logging.LogRecord) -> str:
        """チャット受信 / チャンク受信ログのフォーマット。"""
        event: str = record.chat_event  # type: ignore[attr-defined]
        label = "チャット受信" if event == "recv" else "チャンク受信"
        label_color = _C["green"] if event == "recv" else _C["dim"]
        agent: str = record.chat_agent  # type: ignore[attr-defined]
        preview: str = record.chat_preview  # type: ignore[attr-defined]
        sid: str = getattr(record, "chat_sid", "")
        sid_part = f" {_C['dim']}sid={sid}{_C['reset']}" if sid else ""
        return (
            f"{_C['dim']}{ts}{_C['reset']} "
            f"{_C['bold']}{label_color}{label}{_C['reset']} "
            f"agent={_C['cyan']}{agent}{_C['reset']} "
            f"{_C['yellow']}「{preview}」{_C['reset']}"
            f"{sid_part}"
        )

    def _fmt_tool(self, ts: str, record: logging.LogRecord) -> str:
        """ツール実行ログのフォーマット。"""
        agent: str = record.tool_agent  # type: ignore[attr-defined]
        desc: str  = record.tool_desc   # type: ignore[attr-defined]
        sid: str   = record.tool_sid    # type: ignore[attr-defined]
        return (
            f"{_C['dim']}{ts}{_C['reset']} "
            f"{_C['dim']}ツール実行{_C['reset']} "
            f"agent={_C['cyan']}{agent}{_C['reset']} "
            f"{_C['magenta']}{desc}{_C['reset']} "
            f"{_C['dim']}sid={sid}{_C['reset']}"
        )

    def _fmt_process(self, ts: str, msg: str) -> str:
        """cli_bridge プロセスイベントログのフォーマット。"""
        m = _PROCESS_KW_RE.match(msg)
        if m:
            rest = msg[m.end():]
            return (
                f"{_C['dim']}{ts}{_C['reset']} "
                f"{_C['bold']}{_C['cyan']}{m.group(1)}{_C['reset']}{rest}"
            )
        return f"{_C['dim']}{ts}{_C['reset']} {msg}"

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        # 構造化ログ（extra 属性で判定）
        if hasattr(record, "chat_event"):
            return self._fmt_chat(ts, record)
        if hasattr(record, "tool_agent"):
            return self._fmt_tool(ts, record)

        # cli_bridge プロセスイベント（キーワードマッチ）
        if record.name == "server.cli_bridge" and _PROCESS_KW_RE.match(msg):
            return self._fmt_process(ts, msg)

        # 通常ログ
        lc = _LEVEL_COLOR.get(record.levelno, "")
        mc = _MODULE_COLOR.get(record.name, "")
        name_part = f"{mc}[{record.name}]{_C['reset']}" if mc else f"[{record.name}]"
        msg_part = f"{lc}{msg}{_C['reset']}" if lc else msg
        if record.name in _NO_MODULE_NAME:
            return f"{_C['dim']}{ts}{_C['reset']} {msg_part}"
        return f"{_C['dim']}{ts}{_C['reset']} {name_part} {msg_part}"


class AccessLogFormatter(logging.Formatter):
    """uvicorn アクセスログ用フォーマッター。

    IP・ポート・プロトコルバージョンを除去し、メソッド・パス・ステータスのみ出力する。
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        m = _ACCESS_EXTRACT.search(msg)
        if m:
            method_path = m.group(1)
            status = m.group(2)
            sc = _STATUS_COLOR.get(status[0], "")
            ts = self.formatTime(record, self.datefmt)
            status_part = f"{sc}{status}{_C['reset']}" if sc else status
            return f"{_C['dim']}{ts}{_C['reset']} {method_path} {status_part}"
        return f"{self.formatTime(record, self.datefmt)} {msg}"


class AccessLogFilter(logging.Filter):
    """静的ファイルと高頻度ポーリングのアクセスログを抑制する。"""

    _SKIP = re.compile(
        r'"(?:GET|HEAD) /(?:'
        r'[^"]+\.(?:css|js|ico|png|jpg|gif|webp|woff2?|ttf|svg|map)'  # 静的ファイル
        r'|api/[^/]+/[^/]+/process-status[^"]*'  # プロセス状態ポーリング
        r'|api/[^/]+/[^/]+/tasks[^"]*'           # タスク一覧ポーリング
        r'|api/scheduler/status[^"]*'             # スケジューラー状態ポーリング
        r') HTTP'
    )

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._SKIP.search(record.getMessage())


# ============================================================
# 初期化関数
# ============================================================

_LOG_FMT = ColoredFormatter(datefmt="%Y-%m-%d %H:%M:%S")


def setup_logging() -> None:
    """ルートロガーを初期化する。uvicorn ハンドラはこの時点で未設定のため後でパッチする。"""
    handler = logging.StreamHandler()
    handler.setFormatter(_LOG_FMT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # フィルタはロガーオブジェクト自体に付与するためハンドラ追加後も有効
    logging.getLogger("uvicorn.access").addFilter(AccessLogFilter())


def patch_uvicorn_logging() -> None:
    """uvicorn 起動後にフォーマットを統一する（lifespan 内から呼ぶ）。"""
    access_fmt = AccessLogFormatter(datefmt="%Y-%m-%d %H:%M:%S")
    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.setFormatter(access_fmt)
    for name in ("uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(_LOG_FMT)
