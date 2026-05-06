"""
任务管理 API
后台任务状态查询和管理
"""
from typing import List, Optional
from datetime import datetime
import json

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from web.backend.database import get_db, Task


router = APIRouter()


class TaskResponse(BaseModel):
    id: int
    task_type: str
    status: str
    progress: float
    message: Optional[str]
    result: Optional[dict]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    total: int
    tasks: List[TaskResponse]


def task_to_response(task: Task) -> TaskResponse:
    """转换任务模型到响应"""
    result = None
    if task.result:
        try:
            result = json.loads(task.result)
        except:
            result = {"raw": task.result}

    return TaskResponse(
        id=task.id,
        task_type=task.task_type,
        status=task.status,
        progress=task.progress,
        message=task.message,
        result=result,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    task_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """
    获取任务列表。

    search：在 message 和 result（含 initial_message + 各类 detail JSON）字段上做
    ILIKE %text%。任务的"描述"通常是 result.initial_message，所以同时搜两个字段
    才能覆盖到运行中和已完成两种状态。
    """
    query = db.query(Task)

    if task_type:
        query = query.filter(Task.task_type == task_type)
    if status:
        query = query.filter(Task.status == status)
    if search:
        from sqlalchemy import or_
        # 转义 ILIKE 的通配符：% 和 _ 当字面量看
        s = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        like_pattern = f'%{s}%'
        query = query.filter(or_(
            Task.message.ilike(like_pattern),
            Task.result.ilike(like_pattern),
        ))

    total = query.count()
    tasks = query.order_by(Task.created_at.desc()).offset(offset).limit(limit).all()

    return TaskListResponse(
        total=total,
        tasks=[task_to_response(t) for t in tasks]
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, db: Session = Depends(get_db)):
    """获取任务详情"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return task_to_response(task)


@router.delete("/{task_id}")
async def delete_task(task_id: int, db: Session = Depends(get_db)):
    """删除任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == "running":
        raise HTTPException(status_code=400, detail="无法删除正在运行的任务")

    db.delete(task)
    db.commit()

    return {"message": "任务已删除"}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, db: Session = Depends(get_db)):
    """取消任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ["pending", "running"]:
        raise HTTPException(status_code=400, detail="只能取消待执行或运行中的任务")

    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    task.message = "用户取消"
    db.commit()

    return {"message": "任务已取消"}


def create_task(
    db: Session,
    task_type: str,
    message: str = None,
    params: dict = None,
) -> Task:
    """
    创建新任务。

    把首次 message 同时存到 result.initial_message，update_task_progress 不会动 result，
    所以运行过程中 message 字段被覆盖为进度文本时，详情页仍能从 result 取到原始的"作用范围"信息。

    params：任务输入参数。若任务类型在 task_restart.py 的注册表中，服务重启时会用这些
    参数自动恢复运行；否则只作为元信息存档。dict 必须 JSON 可序列化。
    """
    task = Task(
        task_type=task_type,
        status="pending",
        progress=0.0,
        message=message
    )
    if message:
        task.result = json.dumps({'initial_message': message}, ensure_ascii=False)
    if params is not None:
        task.params = json.dumps(params, ensure_ascii=False, default=str)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def update_task_progress(
    db: Session,
    task_id: int,
    progress: float,
    message: str = None,
    result_patch: dict = None,
):
    """
    更新任务进度。

    result_patch（可选）：把字段合并写入 task.result，用于让长任务在运行中持续输出
    details，详情页就能看到已完成的子项明细，而不是要等 complete_task 才一起出现。

    注意：每次调用都会写一次 DB，调用方应自行限流（如每 N 条记录调用一次）。
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.progress = progress
        if message:
            task.message = message
        if task.status == "pending":
            task.status = "running"
            task.started_at = datetime.utcnow()

        if result_patch:
            merged: dict = {}
            if task.result:
                try:
                    existing = json.loads(task.result)
                    if isinstance(existing, dict):
                        merged.update(existing)
                except Exception:
                    pass
            merged.update(result_patch)
            task.result = json.dumps(merged, ensure_ascii=False)
        db.commit()


def complete_task(
    db: Session,
    task_id: int,
    result: dict = None,
    success: bool = True,
    final_message: str = None,
):
    """
    完成任务。

    合并 result：保留 create_task 时写入的元信息（initial_message 等），
    再用本次传入的 result 字段覆盖同名 key。这样详情页始终能从 result 取到 initial_message。

    final_message：完成时写入 task.message 的最终状态文本。
        - 成功且不传：保留最后一次 update_task_progress 写的 message（可能是中间态如"正在生成报告..."）
        - 失败且不传：自动用 result.error 作为 message
        - 推荐：调用方显式传一个总结，如"扫描完成: N 个视频"
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = "completed" if success else "failed"
        task.progress = 100.0
        task.completed_at = datetime.utcnow()

        # 合并已有 result（含 initial_message）+ 本次传入
        merged: dict = {}
        if task.result:
            try:
                existing = json.loads(task.result)
                if isinstance(existing, dict):
                    merged.update(existing)
            except Exception:
                pass
        if result:
            merged.update(result)
        if merged:
            task.result = json.dumps(merged, ensure_ascii=False)

        # 写入最终状态消息
        if final_message:
            task.message = final_message
        elif not success and result and isinstance(result, dict) and result.get('error'):
            task.message = f"失败: {result['error']}"

        db.commit()
