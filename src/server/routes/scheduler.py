"""スケジューラーAPI"""

from fastapi import APIRouter, Depends

from server.scheduler import Scheduler
from server.routes.deps import get_scheduler

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
def scheduler_status(scheduler: Scheduler = Depends(get_scheduler)):
    """スケジューラーの現在状態を返す"""
    return scheduler.status()


@router.post("/toggle")
def scheduler_toggle(scheduler: Scheduler = Depends(get_scheduler)):
    """ON/OFF切り替え。切り替え後の状態を返す"""
    return scheduler.toggle()


@router.get("/logs")
def scheduler_logs(scheduler: Scheduler = Depends(get_scheduler)):
    """直近100件のスケジューラー実行ログを返す（新しい順）"""
    return scheduler.get_logs()
