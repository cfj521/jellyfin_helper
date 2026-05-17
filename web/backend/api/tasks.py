"""
任务管理 API
后台任务状态查询和管理

实时更新模型：
  - 写入侧：update_task_progress / complete_task / cancel_task / create_task 在 DB commit
    之后调用 _publish_task_update，向 task pubsub 推一份 snapshot。
  - 读取侧：两个 SSE endpoint:
      GET /api/tasks/{id}/stream   订阅 task:{id} 通道（详情页用）
      GET /api/tasks/stream        订阅 tasks:any  通道（列表页用）
    前端 EventSource 收到推送即刷新 UI，无需轮询。
"""
import asyncio
import json
import logging
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from web.backend.database import get_db, Task
from web.backend.services.task_pubsub import TERMINAL_SENTINEL, get_pubsub

logger = logging.getLogger(__name__)
router = APIRouter()


class TaskCancelledError(Exception):
    """协作式取消信号。

    update_task_progress 检测到以下任一情况就抛出：
      - DB 里 task.status == 'cancelled'（用户在前端点了取消）
      - is_shutting_down() 返回 True（进程收到 SIGTERM/SIGINT/Ctrl+C）

    长任务的最外层 try/except 应捕获这个异常，把状态写成 'cancelled' 并返回，
    避免被普通 Exception 分支误写成 'failed'。

    最简易的做法：用 @cancellable_task 装饰器自动处理，省去每个 wrapper 手写。
    """
    def __init__(self, task_id: int, reason: str = 'cancelled'):
        super().__init__(f"task #{task_id} cancelled ({reason})")
        self.task_id = task_id
        self.reason = reason   # 'cancelled' (用户取消) / 'shutdown' (进程关闭)


def cancellable_task(fn):
    """装饰任意 `def run_xxx(task_id, ...)` 形态的后台任务 wrapper。

    捕获 TaskCancelledError → 把 DB 状态写成 cancelled（而不是 failed），
    保留运行中累积的 partial result。其它异常原样抛给 wrapper 内部处理。

    用法：
        @cancellable_task
        def run_subtitle_scan(task_id, paths, recursive, ...):
            ...
    """
    import functools
    @functools.wraps(fn)
    def wrapper(task_id, *args, **kwargs):
        try:
            return fn(task_id, *args, **kwargs)
        except TaskCancelledError as ce:
            from web.backend.database import SessionLocal
            with SessionLocal() as db:
                mark_task_cancelled(db, task_id, reason=ce.reason)
            return None
    return wrapper


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


# ---- SSE 推送辅助：commit 之前构建 snapshot，避免 SQLAlchemy expire 触发再次 SELECT ----

def _build_task_snapshot(task: Task) -> Dict[str, Any]:
    """把内存中的 task 模型序列化成纯 dict（datetime 转 ISO 字符串）。
    必须在 db.commit() 之前调用——commit 会让 task 对象进入 expired 状态，
    再访问属性会触发新的 SELECT（不仅多一次 IO，且大 result JSON 序列化代价不小）。
    """
    return task_to_response(task).model_dump(mode='json')


def _publish_task_update(snapshot: Dict[str, Any], terminal: bool = False) -> None:
    """commit 后调用：推一份 snapshot 给 task:{id} 通道，再推一个轻量 ping 给 tasks:any。
    terminal=True 时额外推 None 哨兵，通知 SSE 关流。"""
    pubsub = get_pubsub()
    task_id = snapshot.get('id')
    if task_id is None:
        return
    pubsub.publish(f'task:{task_id}', snapshot)
    pubsub.publish('tasks:any', {
        'id': task_id,
        'status': snapshot.get('status'),
        'progress': snapshot.get('progress'),
        'task_type': snapshot.get('task_type'),
    })
    if terminal:
        pubsub.publish(f'task:{task_id}', TERMINAL_SENTINEL)


# 用 sync `def`：FastAPI 自动扔 threadpool，避免阻塞 event loop。
# 当后台 worker 频繁 commit 让 PG 繁忙时，async + 同步 SQL 会让所有请求都卡。

@router.get("", response_model=TaskListResponse)
def list_tasks(
    task_type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    include_auto: bool = False,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """
    获取任务列表。

    search：在 message 和 result（含 initial_message + 各类 detail JSON）字段上做
    ILIKE %text%。任务的"描述"通常是 result.initial_message，所以同时搜两个字段
    才能覆盖到运行中和已完成两种状态。

    include_auto：默认 False。会过滤掉 params 中标记 auto_triggered=true 的内部任务
    （例如 LibraryDetail 打开时静默启动的字幕扫描），它们不是用户主动发起的，
    出现在列表里只会干扰。
    """
    query = db.query(Task)

    if task_type:
        query = query.filter(Task.task_type == task_type)
    if status:
        query = query.filter(Task.status == status)
    if not include_auto:
        # params 是 TEXT(JSON)。用 ILIKE 匹配 '"auto_triggered": true' 比 PG JSON ops 更可移植
        # NOT (params ILIKE ...) 在 NULL 上为 NULL，要 OR IS NULL 兜底
        from sqlalchemy import or_, not_
        query = query.filter(or_(
            Task.params.is_(None),
            not_(Task.params.ilike('%"auto_triggered": true%')),
        ))
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


# ---- SSE：必须注册在 /{task_id} 路由之前，否则 "stream" 会被当成 task_id 走 int 校验 ----

_SSE_HEADERS = {
    'Cache-Control': 'no-cache, no-transform',
    'X-Accel-Buffering': 'no',  # 关闭 nginx 缓冲，让 chunk 即时推到浏览器
    'Connection': 'keep-alive',
}
_KEEPALIVE_INTERVAL_SEC = 25  # 客户端 / 反向代理通常 30-60s 超时，25s 心跳留余量


async def _async_queue_get(q: Queue, timeout: float):
    """把同步阻塞的 queue.get(timeout=...) 包成 async；超时不抛异常，返回 _SENTINEL_TIMEOUT。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _queue_get_or_timeout, q, timeout)


_SENTINEL_TIMEOUT = object()


def _queue_get_or_timeout(q: Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except Empty:
        return _SENTINEL_TIMEOUT


@router.get("/stream")
async def tasks_stream(request: Request):
    """SSE 列表页通道：任意 task 状态变化时推一个轻量 ping，前端收到后自行 re-fetch /api/tasks。

    选择"ping + re-fetch"而非"全量列表推送"：列表受分页 / 过滤参数影响，
    服务端不知道客户端当前的查询条件；推 ping 让客户端用自己的参数拉就行。
    """
    pubsub = get_pubsub()
    queue = pubsub.subscribe('tasks:any')

    from web.backend.shutdown import is_shutting_down

    async def gen():
        try:
            # 连上就发一个 ping，让前端立即做一次基线刷新
            yield 'event: ping\ndata: {}\n\n'
            while True:
                if is_shutting_down() or await request.is_disconnected():
                    break
                item = await _async_queue_get(queue, _KEEPALIVE_INTERVAL_SEC)
                if item is _SENTINEL_TIMEOUT:
                    yield ': keepalive\n\n'  # SSE 注释行，浏览器忽略
                    continue
                yield f'event: ping\ndata: {json.dumps(item, default=str)}\n\n'
        finally:
            pubsub.unsubscribe('tasks:any', queue)

    return StreamingResponse(gen(), media_type='text/event-stream', headers=_SSE_HEADERS)


@router.get("/{task_id}/stream")
async def task_stream(task_id: int, request: Request):
    """SSE 详情页通道：先发一份初始 snapshot（来自 DB），之后由 pubsub 推增量。

    终态（completed / failed / cancelled）发送后跟一个 'done' event，前端可关闭
    连接避免无谓重连。
    """
    pubsub = get_pubsub()
    channel = f'task:{task_id}'
    # 关键顺序：先订阅，再读 DB。
    # 反过来的话，"DB 读取" → "订阅" 之间的窗口里如果发生了更新，订阅前的事件会丢。
    queue = pubsub.subscribe(channel)

    from web.backend.shutdown import is_shutting_down

    async def gen():
        try:
            # 1. 初始 snapshot（从 DB 一次性读出）
            from web.backend.database import SessionLocal
            already_terminal = False
            with SessionLocal() as db:
                task = db.query(Task).filter(Task.id == task_id).first()
                if not task:
                    yield f'event: error\ndata: {json.dumps({"error": "task not found"})}\n\n'
                    return
                snapshot = _build_task_snapshot(task)
                already_terminal = task.status in ('completed', 'failed', 'cancelled')
            yield f'data: {json.dumps(snapshot, default=str)}\n\n'

            if already_terminal:
                yield 'event: done\ndata: {}\n\n'
                return

            # 2. 流式推送后续更新
            while True:
                if is_shutting_down() or await request.is_disconnected():
                    break
                item = await _async_queue_get(queue, _KEEPALIVE_INTERVAL_SEC)
                if item is _SENTINEL_TIMEOUT:
                    yield ': keepalive\n\n'
                    continue
                if item is TERMINAL_SENTINEL:
                    yield 'event: done\ndata: {}\n\n'
                    break
                yield f'data: {json.dumps(item, default=str)}\n\n'
        finally:
            pubsub.unsubscribe(channel, queue)

    return StreamingResponse(gen(), media_type='text/event-stream', headers=_SSE_HEADERS)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: int, db: Session = Depends(get_db)):
    """获取任务详情"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return task_to_response(task)


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """删除任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == "running":
        raise HTTPException(status_code=400, detail="无法删除正在运行的任务")

    logger.info(
        f"/tasks/delete: id={task_id} task_type={task.task_type!r} status={task.status!r}"
    )
    db.delete(task)
    db.commit()

    return {"message": "任务已删除"}


def _extract_child_task_ids(task: Task) -> List[int]:
    """从 run_all 父任务的 result.steps 中提取所有子任务 ID。"""
    if not task.result:
        return []
    try:
        data = json.loads(task.result)
        steps = data.get('steps') or []
        return [s['task_id'] for s in steps if isinstance(s, dict) and s.get('task_id')]
    except Exception:
        return []


@router.post("/{task_id}/cancel")
def cancel_task(task_id: int, db: Session = Depends(get_db)):
    """取消任务"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ["pending", "running"]:
        raise HTTPException(status_code=400, detail="只能取消待执行或运行中的任务")

    logger.warning(
        f"/tasks/cancel: id={task_id} task_type={task.task_type!r} "
        f"status={task.status!r} progress={task.progress}"
    )
    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    task.message = "用户取消"
    # commit 前抓 snapshot，避免 expire 后再 SELECT
    snapshot = _build_task_snapshot(task)
    db.commit()
    _publish_task_update(snapshot, terminal=True)

    # run_all 类型的父任务：同时取消所有正在运行的子任务
    # result.steps 里记录了每个子任务的 task_id
    child_cancelled = 0
    if task.task_type == 'run_all':
        child_ids = _extract_child_task_ids(task)
        for cid in child_ids:
            child = db.query(Task).filter(Task.id == cid).first()
            if child and child.status in ('pending', 'running'):
                child.status = 'cancelled'
                child.completed_at = datetime.utcnow()
                child.message = '父任务取消'
                cs = _build_task_snapshot(child)
                db.commit()
                _publish_task_update(cs, terminal=True)
                child_cancelled += 1
        if child_cancelled:
            logger.info(f"run_all #{task_id}: 级联取消 {child_cancelled} 个子任务")

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
    # 通知列表页有新任务（详情通道暂无订阅者，发了也无害）
    _publish_task_update(_build_task_snapshot(task), terminal=False)
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

    顺带：把当前 worker 线程关联到此 task_id（task_log_capture），后续 WARNING+ 级别
    日志会自动归集到这个 task 的 result.warnings；并把已积累的 warnings 一并合并
    写入 result（让前端运行中也能看到）。

    注意：每次调用都会写一次 DB，调用方应自行限流（如每 N 条记录调用一次）。
    """
    # 先 attach 当前 worker 线程，让接下来的 logger.warning 等都被关联到本 task
    from web.backend.task_log_capture import attach as _log_attach, peek as _log_peek
    _log_attach(task_id)
    captured = _log_peek(task_id)

    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        # 协作式取消检查：这是几乎所有长任务循环里都会调到的"枢纽"，
        # 在这里抛 TaskCancelledError 能让任务在下一次 emit progress 时干净退出。
        # 顺序：先看 DB（用户取消优先级最高），再看进程级 shutdown。
        if task.status == 'cancelled':
            logger.info(f"task #{task_id} detected cancelled status, raising TaskCancelledError")
            raise TaskCancelledError(task_id, 'cancelled')
        from web.backend.shutdown import is_shutting_down
        if is_shutting_down():
            logger.info(f"task #{task_id} detected shutdown signal, raising TaskCancelledError")
            raise TaskCancelledError(task_id, 'shutdown')

        task.progress = progress
        if message:
            task.message = message
        if task.status == "pending":
            task.status = "running"
            task.started_at = datetime.utcnow()

        if result_patch or captured:
            merged: dict = {}
            if task.result:
                try:
                    existing = json.loads(task.result)
                    if isinstance(existing, dict):
                        merged.update(existing)
                except Exception:
                    pass
            if result_patch:
                merged.update(result_patch)
            if captured:
                merged['warnings'] = captured
            task.result = json.dumps(merged, ensure_ascii=False)
        # commit 前抓 snapshot——commit 后 task 会进入 expired，再访问属性会触发新 SELECT
        snapshot = _build_task_snapshot(task)
        db.commit()
        _publish_task_update(snapshot, terminal=False)


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
    # 取出本 task 累积的 WARNING+ 日志，并丢弃缓冲（task 完结）
    from web.backend.task_log_capture import drain as _log_drain, detach as _log_detach
    captured = _log_drain(task_id)

    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        # 任务运行时长 + 终态：所有后台 worker 完成都过这里，是 task 状态机的统一出口
        # 比每个 worker 自己写 "任务 X 完成" 更可靠（漏写不会丢日志）
        elapsed_str = ''
        if task.started_at:
            elapsed = (datetime.utcnow() - task.started_at).total_seconds()
            elapsed_str = f" elapsed={elapsed:.1f}s"
        log_msg = (
            f"task complete: id={task_id} type={task.task_type!r} "
            f"success={success}{elapsed_str}"
        )
        if not success:
            # 失败时多打一行 err 摘要，方便 grep 'task complete.*success=False' 看故障批次
            err = (result or {}).get('error') if isinstance(result, dict) else None
            if err:
                log_msg += f" error={str(err)[:200]!r}"
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        task.status = "completed" if success else "failed"
        task.progress = 100.0
        task.completed_at = datetime.utcnow()

        # 合并已有 result（含 initial_message）+ 本次传入 + 捕获的 warnings
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
        if captured:
            # 合并 update_task_progress 期间已写入的 warnings + 最后一次 drain 的
            existing_warns = merged.get('warnings') or []
            seen = {(w.get('ts'), w.get('msg')) for w in existing_warns}
            for w in captured:
                if (w.get('ts'), w.get('msg')) not in seen:
                    existing_warns.append(w)
            merged['warnings'] = existing_warns
        if merged:
            task.result = json.dumps(merged, ensure_ascii=False)

        # 写入最终状态消息
        if final_message:
            task.message = final_message
        elif not success and result and isinstance(result, dict) and result.get('error'):
            task.message = f"失败: {result['error']}"

        # commit 前抓 snapshot，commit 后推 + 关 SSE 流
        snapshot = _build_task_snapshot(task)
        db.commit()
        _publish_task_update(snapshot, terminal=True)
    _log_detach()


def mark_task_cancelled(
    db: Session,
    task_id: int,
    reason: str = 'cancelled',
    partial_result: dict = None,
):
    """把任务标为 cancelled 状态。任务体捕获 TaskCancelledError 后调用。

    跟 complete_task(success=False) 的区别：
      - status='cancelled' 而不是 'failed'
      - message 自动写"用户取消" / "进程关闭中止"
      - 保留 update_task_progress 期间累积的 partial result（不强行覆盖）
    """
    from web.backend.task_log_capture import drain as _log_drain, detach as _log_detach
    captured = _log_drain(task_id)

    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        elapsed_str = ''
        if task.started_at:
            elapsed = (datetime.utcnow() - task.started_at).total_seconds()
            elapsed_str = f" elapsed={elapsed:.1f}s"
        logger.info(
            f"task cancelled: id={task_id} type={task.task_type!r} reason={reason}{elapsed_str}"
        )

        task.status = "cancelled"
        task.completed_at = datetime.utcnow()
        # cancel 时 progress 保留当时值，不强写 100；前端能看出"中断在某个进度"
        task.message = "用户取消" if reason == 'cancelled' else "进程关闭中止"

        # 合并已有 result + partial（保留运行中的 details 给用户看）
        merged: dict = {}
        if task.result:
            try:
                existing = json.loads(task.result)
                if isinstance(existing, dict):
                    merged.update(existing)
            except Exception:
                pass
        if partial_result:
            merged.update(partial_result)
        merged['cancel_reason'] = reason
        if captured:
            existing_warns = merged.get('warnings') or []
            seen = {(w.get('ts'), w.get('msg')) for w in existing_warns}
            for w in captured:
                if (w.get('ts'), w.get('msg')) not in seen:
                    existing_warns.append(w)
            merged['warnings'] = existing_warns
        if merged:
            task.result = json.dumps(merged, ensure_ascii=False)

        snapshot = _build_task_snapshot(task)
        db.commit()
        _publish_task_update(snapshot, terminal=True)
    _log_detach()
