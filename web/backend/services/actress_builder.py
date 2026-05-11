"""
女优库 lazy 构建服务（单例后台线程）。

工作流程：
  1) discover：扫 AdultItem.actors[] 找出所有出场名字，
     不在 adult_actresses（jp/zh/en/aliases 任意命中）里的就插入一条
     占位行（jp_name=查询名, source='pending'）。
  2) resolve：循环挑一条 source='pending' 的，调 ActressResolver。
     - 找到 → 升级占位行为完整数据；如果该 javdb_id 早已被别的行占据
       （说明是别名），把当前 query 并入那行的 aliases 里，删掉重复占位
     - 没找到 → 标记 source='not_found'，跳过重试
  3) 每次解析后 sleep request_delay 秒，给"保守爬取"留余量。
  4) stop_event 一打就退出，可以安全 ctrl-C / 前端点停。

DB 用短事务（每条 name 一个），不持有连接跨 HTTP。
"""
import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

from sqlalchemy import or_

logger = logging.getLogger(__name__)


class ActressBuilder:
    """女优库构建器（单例）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state: Dict = {
            'running': False,
            'started_at': None,
            'last_action_at': None,
            'request_delay': 5.0,
            'current_query': None,
            'current_phase': None,        # discover / resolve / idle / stopped
            'resolved_in_run': 0,
            'not_found_in_run': 0,
            'merged_in_run': 0,
            'errors_in_run': 0,
            'last_error': None,
        }

    # ============ 公开 API ============

    def status(self) -> Dict:
        from web.backend.database import SessionLocal, AdultActress
        with SessionLocal() as db:
            total = db.query(AdultActress).count()
            resolved = db.query(AdultActress).filter(
                AdultActress.source.notin_(['pending', 'not_found']),
                AdultActress.source.isnot(None),
            ).count()
            pending = db.query(AdultActress).filter(AdultActress.source == 'pending').count()
            not_found = db.query(AdultActress).filter(AdultActress.source == 'not_found').count()
        with self._lock:
            s = dict(self._state)
        s.update({
            'total': total,
            'resolved': resolved,
            'pending': pending,
            'not_found': not_found,
        })
        return s

    def start(self, request_delay: float = 5.0) -> bool:
        """启动后台线程；已在跑则返回 False。"""
        with self._lock:
            if self._state['running']:
                return False
            self._stop_event.clear()
            self._state.update({
                'running': True,
                'started_at': datetime.utcnow().isoformat() + 'Z',
                'request_delay': max(1.0, float(request_delay)),
                'current_query': None,
                'current_phase': 'starting',
                'resolved_in_run': 0,
                'not_found_in_run': 0,
                'merged_in_run': 0,
                'errors_in_run': 0,
                'last_error': None,
            })
            t = threading.Thread(target=self._run, name='ActressBuilder', daemon=True)
            t.start()
            self._thread = t
        logger.info(f"ActressBuilder 启动（delay={request_delay}s）")
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self._state['running']:
                return False
            self._stop_event.set()
        logger.info("ActressBuilder 收到停止请求")
        return True

    # ============ 主循环 ============

    def _run(self):
        from tools.adult_manager.actress_resolver import ActressResolver

        try:
            resolver = ActressResolver(request_delay=self._state['request_delay'])
        except RuntimeError as e:
            self._set_error(f"resolver 初始化失败: {e}")
            self._mark_stopped()
            return

        try:
            # ---- Phase 0: 把之前 not_found 的重置回 pending（继续处理没完成的）----
            # 用户语义：点"开始构建"= 继续处理所有没解析完的（含上次失败/未找到的）
            # 不重置就会出现"点了没反应" —— 所有名字非 resolved 即 not_found，pending 早是 0
            self._update_phase('discover')
            requeued = self._requeue_not_found()
            if requeued:
                logger.info(f"ActressBuilder 重新排队 {requeued} 条 not_found → pending")

            # ---- Phase 1: discover ----
            added = self._discover()
            logger.info(f"ActressBuilder discover 完成，新增 {added} 条 pending")

            # ---- Phase 2: resolve loop ----
            self._update_phase('resolve')
            while not self._stop_event.is_set():
                next_row = self._take_one_pending()
                if next_row is None:
                    logger.info("ActressBuilder 没有 pending 项了，进入 idle")
                    self._update_phase('idle')
                    break

                row_id, query = next_row
                self._update_state(current_query=query, last_action_at=datetime.utcnow().isoformat() + 'Z')
                try:
                    self._resolve_one(resolver, row_id, query)
                except Exception as e:
                    logger.exception(f"resolve {query} 失败")
                    self._set_error(str(e))
                    self._inc('errors_in_run')

                # request_delay 已经在 resolver._wait 里实现，但循环之间也插一个软等待
                # （如果 resolver 命中缓存极快，避免太密）—— 半秒兜底
                if self._stop_event.wait(0.5):
                    break

        finally:
            self._mark_stopped()

    # ============ 阶段实现 ============

    def _requeue_not_found(self) -> int:
        """把所有 source='not_found' 的行重置回 'pending'，让本轮重新尝试。
        点"开始构建"应该让用户感觉是"继续处理没完成的"，所以上一轮失败的也算未完成。
        """
        from web.backend.database import SessionLocal, AdultActress
        with SessionLocal() as db:
            rows = (db.query(AdultActress)
                      .filter(AdultActress.source == 'not_found')
                      .all())
            count = len(rows)
            if not count:
                return 0
            for r in rows:
                r.source = 'pending'
            db.commit()
            return count

    def _discover(self) -> int:
        """扫 AdultItem.actors[]，把没见过的名字插入为 pending 占位行。
        命中规则：与 AdultActress 的 jp_name / zh_name / en_name / aliases(JSON) 任一相等即视为已知。
        """
        from web.backend.database import SessionLocal, AdultItem, AdultActress

        added = 0
        with SessionLocal() as db:
            # 1. 拉所有出场名字（去重）
            seen: Set[str] = set()
            actor_jsons = db.query(AdultItem.actors).filter(AdultItem.actors.isnot(None)).all()
            for (j,) in actor_jsons:
                try:
                    arr = json.loads(j)
                except Exception:
                    continue
                if not isinstance(arr, list):
                    continue
                for n in arr:
                    if isinstance(n, str):
                        n = n.strip()
                        if n:
                            seen.add(n)

            if not seen:
                return 0

            # 2. 拉 adult_actresses 已知的所有名字
            known: Set[str] = set()
            for r in db.query(AdultActress.jp_name, AdultActress.zh_name,
                              AdultActress.en_name, AdultActress.aliases).all():
                jp, zh, en, al = r
                if jp: known.add(jp)
                if zh: known.add(zh)
                if en: known.add(en)
                if al:
                    try:
                        for x in json.loads(al) or []:
                            if isinstance(x, str):
                                known.add(x.strip())
                    except Exception:
                        pass

            # 3. 差集 → 插占位
            todo = sorted(seen - known)
            for name in todo:
                if self._stop_event.is_set():
                    break
                try:
                    db.add(AdultActress(jp_name=name, source='pending'))
                    db.commit()
                    added += 1
                except Exception:
                    db.rollback()  # UNIQUE 冲突等，忽略
        return added

    def _take_one_pending(self):
        """取一条 source='pending' 的行 → 返回 (id, jp_name)。无则 None。"""
        from web.backend.database import SessionLocal, AdultActress
        with SessionLocal() as db:
            row = (db.query(AdultActress.id, AdultActress.jp_name)
                     .filter(AdultActress.source == 'pending')
                     .order_by(AdultActress.id.asc())
                     .first())
            if not row:
                return None
            return (row[0], row[1])

    def _resolve_one(self, resolver, row_id: int, query: str):
        """对单条 pending 行调 resolver，写回结果。"""
        from web.backend.database import SessionLocal, AdultActress

        # —— HTTP 阶段，不持 DB ——
        result = resolver.resolve(query)

        with SessionLocal() as db:
            row = db.query(AdultActress).filter(AdultActress.id == row_id).first()
            if not row:
                return  # 期间被外部删了

            if result is None:
                row.source = 'not_found'
                db.commit()
                self._inc('not_found_in_run')
                logger.info(f"[{query}] not_found")
                return

            # 检查是否已存在同 javdb_id 的另一条（说明 query 是别名）
            existing = (db.query(AdultActress)
                          .filter(AdultActress.javdb_id == result.javdb_id,
                                  AdultActress.id != row_id)
                          .first())

            if existing:
                # 合并：query 进 existing.aliases，删占位
                aliases = []
                if existing.aliases:
                    try:
                        aliases = list(json.loads(existing.aliases) or [])
                    except Exception:
                        aliases = []
                canonical = {existing.jp_name, existing.zh_name, existing.en_name} - {None}
                added_alias = False
                for n in [query] + list(result.aliases or []):
                    if n and n not in canonical and n not in aliases:
                        aliases.append(n)
                        added_alias = True
                if added_alias:
                    existing.aliases = json.dumps(aliases, ensure_ascii=False)
                db.delete(row)
                db.commit()
                self._inc('merged_in_run')
                logger.info(f"[{query}] merged into javdb_id={result.javdb_id} (existing id={existing.id})")
                return

            # 也要查同 jp_name 的其它行（discover 时 query="葵つかさ" 和 query="葵司"
            # 会建两个占位；resolve "葵司" 后 jp_name 该被改成"葵つかさ"，撞上前者）
            same_jp = (db.query(AdultActress)
                         .filter(AdultActress.jp_name == result.jp_name,
                                 AdultActress.id != row_id)
                         .first())
            if same_jp:
                # 把当前 row 当作 same_jp 的别名合并
                aliases = []
                if same_jp.aliases:
                    try:
                        aliases = list(json.loads(same_jp.aliases) or [])
                    except Exception:
                        aliases = []
                canonical = {same_jp.jp_name, same_jp.zh_name, same_jp.en_name} - {None}
                changed = False
                for n in [query] + list(result.aliases or []):
                    if n and n not in canonical and n not in aliases:
                        aliases.append(n)
                        changed = True
                # 如果 same_jp 没 javdb_id（之前 pending 时建的），趁机回填
                if not same_jp.javdb_id:
                    same_jp.javdb_id = result.javdb_id
                    if not same_jp.zh_name and result.zh_name:
                        same_jp.zh_name = result.zh_name
                    if not same_jp.en_name and result.en_name:
                        same_jp.en_name = result.en_name
                    if not same_jp.avatar_url and result.avatar_url:
                        same_jp.avatar_url = result.avatar_url
                    same_jp.source = 'javdb'
                    changed = True
                if changed and aliases:
                    same_jp.aliases = json.dumps(aliases, ensure_ascii=False)
                db.delete(row)
                db.commit()
                self._inc('merged_in_run')
                logger.info(f"[{query}] merged into existing jp_name={result.jp_name}")
                return

            # 升级当前占位行
            row.jp_name = result.jp_name
            row.zh_name = result.zh_name
            row.en_name = result.en_name
            row.avatar_url = result.avatar_url
            row.javdb_id = result.javdb_id
            row.source = 'javdb'
            aliases_list = list(result.aliases or [])
            canonical = {result.jp_name, result.zh_name, result.en_name} - {None}
            if query not in canonical and query not in aliases_list:
                aliases_list.append(query)
            if aliases_list:
                row.aliases = json.dumps(aliases_list, ensure_ascii=False)
            db.commit()
            self._inc('resolved_in_run')
            logger.info(f"[{query}] resolved → {result.jp_name} / {result.zh_name} (javdb_id={result.javdb_id})")

    # ============ 状态辅助 ============

    def _update_phase(self, phase: str):
        with self._lock:
            self._state['current_phase'] = phase

    def _update_state(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _inc(self, key: str):
        with self._lock:
            self._state[key] = self._state.get(key, 0) + 1
            self._state['last_action_at'] = datetime.utcnow().isoformat() + 'Z'

    def _set_error(self, msg: str):
        with self._lock:
            self._state['last_error'] = msg

    def _mark_stopped(self):
        with self._lock:
            self._state['running'] = False
            self._state['current_phase'] = 'stopped'
            self._state['current_query'] = None
        self._stop_event.set()
        logger.info("ActressBuilder 已停止")


# 全局单例
builder = ActressBuilder()
