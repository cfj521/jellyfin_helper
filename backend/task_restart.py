"""
任务重启与孤儿清理。

服务重启时，DB 里 status 为 pending/running 的任务都是"孤儿"（执行它们的线程
已死）。本模块在启动时扫表，按以下规则处理：

  - 任务类型已通过 @register_resumable 注册 + DB 里有 params → 重置为 pending，
    起新线程重跑，把 task.message 标注为"服务重启已自动恢复"
  - 否则 → 标记 failed，task.message 写入中断原因

注册可恢复的任务类型时，被注册函数必须满足：
  1. 第一个位置参数是 task_id: int
  2. 其余参数全部用关键字传入（注册时声明的 param_keys 列表会决定从 DB params
     dict 抽哪些 key 当 kwargs）
  3. 任务本身**幂等**或**有跳过已完成项的能力**（如：只读扫描、跳过已修复的演员/海报、
     文件已存在则跳过的下载等）

不要给写文件/删文件/重命名/单条任务/run_all 父任务这类副作用强的类型注册。
"""
import json
import logging
import threading
from datetime import datetime
from typing import Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)


# task_type -> (entry_function, ordered_param_keys)
_RESUME_REGISTRY: Dict[str, Tuple[Callable, List[str]]] = {}


def register_resumable(task_type: str, param_keys: List[str]):
    """装饰器：注册一个可在服务重启后自动恢复的任务入口函数。"""
    def deco(fn):
        _RESUME_REGISTRY[task_type] = (fn, list(param_keys))
        return fn
    return deco


def is_resumable(task_type: str) -> bool:
    return task_type in _RESUME_REGISTRY


def restart_orphan_tasks() -> dict:
    """启动时调用一次。返回统计：{resumed: N, failed: M, skipped: K}。"""
    from backend.database import SessionLocal, Task

    stats = {"resumed": 0, "failed": 0}
    db = SessionLocal()
    try:
        orphans = db.query(Task).filter(Task.status.in_(["pending", "running"])).all()
        if not orphans:
            return stats

        logger.info(f"发现 {len(orphans)} 个孤儿任务（重启前未完成）")

        for task in orphans:
            handler = _RESUME_REGISTRY.get(task.task_type)
            params: dict = {}
            if task.params:
                try:
                    parsed = json.loads(task.params)
                    if isinstance(parsed, dict):
                        params = parsed
                except Exception:
                    params = {}

            # 不可恢复或缺少参数 → 标 failed
            if handler is None:
                _mark_failed(task, f"服务重启中断（任务类型 {task.task_type} 不支持自动恢复）")
                stats["failed"] += 1
                continue
            if not params:
                _mark_failed(task, "服务重启中断（缺少恢复参数）")
                stats["failed"] += 1
                continue

            entry, param_keys = handler
            kwargs = {k: params[k] for k in param_keys if k in params}

            try:
                # 重置为 pending；entry 函数内部会把它推进到 running
                task.status = "pending"
                task.progress = 0.0
                task.message = "服务重启，正在自动恢复..."
                task.started_at = None
                task.completed_at = None
                db.commit()

                _spawn_worker(entry, task.id, kwargs)
                logger.info(f"  任务 #{task.id} ({task.task_type}) → 已重启")
                stats["resumed"] += 1
            except Exception as e:
                logger.exception(f"  任务 #{task.id} ({task.task_type}) 重启失败")
                _mark_failed(task, f"服务重启恢复失败: {e}")
                stats["failed"] += 1

        db.commit()
        logger.info(
            f"孤儿任务处理完成：恢复 {stats['resumed']}，失败 {stats['failed']}"
        )
        return stats
    finally:
        db.close()


def _mark_failed(task, reason: str):
    task.status = "failed"
    task.completed_at = datetime.utcnow()
    task.message = reason


def _spawn_worker(entry: Callable, task_id: int, kwargs: dict):
    """开 daemon 线程跑任务（与 FastAPI BackgroundTasks 行为一致：进程退出即终止）。"""
    t = threading.Thread(
        target=_safe_run,
        args=(entry, task_id, kwargs),
        name=f"task-restart-{task_id}",
        daemon=True,
    )
    t.start()


def _safe_run(entry: Callable, task_id: int, kwargs: dict):
    """包一层异常捕获：恢复线程崩溃时把任务标 failed，不影响其他任务。"""
    try:
        entry(task_id, **kwargs)
    except Exception as e:
        logger.exception(f"恢复任务 #{task_id} 运行时崩溃")
        from backend.database import SessionLocal, Task
        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task and task.status not in ("completed", "failed"):
                _mark_failed(task, f"恢复运行崩溃: {e}")
                db.commit()
        finally:
            db.close()
