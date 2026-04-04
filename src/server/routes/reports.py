"""レポート関連API — エージェントディレクトリ配下の .md ファイルを返す"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from server.config import AgentNotFoundError, ConfigManager
from server.routes.deps import get_config_manager

router = APIRouter(prefix="/api/agents/{agent_id}/reports", tags=["reports"])

# スキャン除外ディレクトリ（隠しディレクトリ + よく大きいもの）
_EXCLUDE_DIRS = {".git", ".claude", ".kobito", ".pytest_cache", ".playwright-mcp",
                 "node_modules", "__pycache__", ".venv", "venv"}


def _scan_md_files(root: Path) -> list[dict]:
    """root 配下の .md ファイルを再帰的に収集する（除外ディレクトリをスキップ）"""
    results = []
    try:
        for p in root.rglob("*.md"):
            # 除外ディレクトリを含むパスはスキップ
            if any(part in _EXCLUDE_DIRS or part.startswith(".") for part in p.parts[len(root.parts):]):
                continue
            try:
                st = p.stat()
                results.append({
                    "filename": str(p.relative_to(root)),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
            except OSError:
                continue
    except OSError:
        pass
    return sorted(results, key=lambda x: x["mtime"], reverse=True)


@router.get("")
def list_reports(
    agent_id: str,
    config: ConfigManager = Depends(get_config_manager),
):
    """{agent.path}/ 配下の .md ファイル一覧を返す（新しい順）"""
    try:
        agent = config.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail=f"エージェント '{agent_id}' が見つかりません")
    return _scan_md_files(Path(agent.path))


@router.get("/{filepath:path}")
def get_report(
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
    if not target.exists() or not target.is_file() or target.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))
