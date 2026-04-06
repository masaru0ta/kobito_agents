"""ファイルブラウザAPI — エージェントディレクトリ配下のファイル/ディレクトリを返す"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from server.config import AgentNotFoundError, ConfigManager
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents/{agent_id}/reports", tags=["reports"])

# 表示除外ディレクトリ（隠しディレクトリ + ノイズになりやすいもの）
_EXCLUDE_DIRS = {".git", ".pytest_cache", ".playwright-mcp",
                 "node_modules", "__pycache__", ".venv", "venv"}


def _is_excluded(name: str) -> bool:
    return name in _EXCLUDE_DIRS


@router.get("")
def list_dir(
    agent_id: str,
    path: str = Query(default=""),
    config: ConfigManager = Depends(get_config_manager),
):
    """指定パスのディレクトリ一覧を返す。path="" でルート。"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")

    root = Path(agent.path).resolve()
    target = (root / path).resolve() if path else root

    # パストラバーサル防止
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=400, detail="不正なパス")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="ディレクトリが見つかりません")

    dirs = []
    files = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir():
                if _is_excluded(entry.name):
                    continue
                latest = 0.0
                try:
                    for child in entry.iterdir():
                        try:
                            t = child.stat().st_mtime
                            if t > latest:
                                latest = t
                        except OSError:
                            pass
                except OSError:
                    pass
                dirs.append({
                    "name": entry.name,
                    "path": str(entry.relative_to(root)).replace("\\", "/"),
                    "mtime": latest or entry.stat().st_mtime,
                })
            elif entry.is_file():
                try:
                    st = entry.stat()
                    suffix = entry.suffix.lower()
                    is_md = suffix == ".md"
                    _CODE_SUFFIXES = {
                        ".py", ".js", ".ts", ".jsx", ".tsx", ".css",
                        ".sh", ".bash", ".yaml", ".yml", ".toml", ".txt",
                        ".go", ".rs", ".java", ".cpp", ".c", ".h", ".rb", ".php",
                    }
                    is_code = suffix in _CODE_SUFFIXES
                    preview = ""
                    if is_md or is_code:
                        try:
                            with entry.open(encoding="utf-8", errors="replace") as fh:
                                first_line = True
                                fallback = ""
                                for line in fh:
                                    raw = line.strip()
                                    if not raw:
                                        continue
                                    if first_line and raw == "---":
                                        first_line = False
                                        continue
                                    first_line = False
                                    # 記号を除いた内容
                                    content = raw.lstrip("/*!*/#- \t").strip()
                                    if not content:
                                        continue
                                    # 日本語を含む行を優先
                                    if any('\u3000' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef' for c in content):
                                        preview = content
                                        break
                                    # フォールバック: 最初の意味ある行
                                    if not fallback and content != entry.name:
                                        fallback = content
                                if not preview:
                                    preview = fallback
                        except OSError:
                            pass
                    files.append({
                        "name": entry.name,
                        "path": str(entry.relative_to(root)).replace("\\", "/"),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "is_md": is_md,
                        "is_image": suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"},
                        "is_json": suffix == ".json",
                        "is_code": is_code,
                        "preview": preview,
                    })
                except OSError:
                    continue
    except OSError:
        pass

    return {"dirs": dirs, "files": files}


@router.get("/{filepath:path}")
def get_file(
    agent_id: str,
    filepath: str,
    config: ConfigManager = Depends(get_config_manager),
):
    """指定された .md ファイルの内容を返す"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    root = Path(agent.path).resolve()
    target = (root / filepath).resolve()
    # パストラバーサル防止
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=400, detail="不正なファイルパス")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    suffix = target.suffix.lower()
    if suffix in {".html", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return FileResponse(target)
    _TEXT_SUFFIXES = {
        ".md", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".css",
        ".sh", ".bash", ".yaml", ".yml", ".toml", ".txt",
        ".go", ".rs", ".java", ".cpp", ".c", ".h", ".rb", ".php",
    }
    if suffix in _TEXT_SUFFIXES:
        return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))
    raise HTTPException(status_code=400, detail="プレビュー非対応のファイル形式")
