"""
集中式配额保护 —— 所有外部 API 请求源统一在此登记限制规则。

设计目标：
  1. 各 client 自身实例级 delay 仍是第一道闸（保持现有 _wait/_rate_limit 不变）
  2. 本模块作为**进程级保底层**：
     - 被动：客户端收到 429/限频信号 → report_limited() 触发暂停
     - 主动：客户端每次请求前 acquire() 检查暂停 / 批量配额
  3. **batch 调用额外受配额约束**（分钟/小时/日滑动窗）
     ── 谁算 batch？UI 批量动作按钮（修海报、修演员…）+ 后台 worker
     ── 普通前台单条请求 batch=False，只受 hard delay + external_pause 控制
        （internal_pause 仅在 batch=True 时检查本地配额窗口才会触发）
  4. 进程全局单例，线程安全
  5. 日志可观察：触发暂停/熔断/配额耗尽时 WARN；恢复时 INFO

接入方式（两层）：
  A. 被动上报：client 收到 429 / 限频错误后
       quota_guard.report_limited('assrt')
  B. 主动检查：client 每次请求前
       quota_guard.acquire('tmdb', batch=self.batch)
     如果当前已被暂停或撞到 batch 配额则 sleep 后返回等待时长。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
源              │ 官方 limits                    │ hard delay │ batch 配额          │ int pause / ext pause
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TMDB            │ ~50 req/10s (≈5 req/s)         │ 0.5s       │ 30/min 900/h 5000/d │ 60s  / 15min
assrt           │ 20 req/min (token+IP)          │ 6.0s       │ 10/min 500/h 2500/d │ 120s / 30min
OpenSubtitles   │ search: 5 req/10s              │ 3.0s       │ 12/min 600/h 3000/d │ 120s / 30min
                │ download: **20 / 24h (免费版)**│            │                     │ (+ 命中 406 时 pause_for 到 reset_time_utc)
Shooter         │ 无公开                         │ 3.0s       │ 10/min 500/h 2500/d │ 120s / 30min
MDB List        │ 1000 / day                     │ 1.0s       │ 20/min 300/h 1000/d │ 180s   / 1d
Douban          │ 无公开（403/429/503 反爬）     │ 5.0s       │ 10/min 300/h 2000/d │ 180s / 2h
Trakt           │ 未明确（低频可用）             │ 1.0s       │ ——                  │ 60s   / 15min
AniList         │ 90 req/min                     │ 1.0s       │ ——                  │ 120s   / 30min
Wikidata        │ 未明确（要求合规 UA）          │ 1.0s       │ 15/min 600/h 3000/d │ 60s  / 30min
Adult Scraper   │ 各站独立（多为 Cloudflare 拦） │ 3.0s       │ 15/min 600/h 3000/d │ 180s / 30min
LLM             │ 取决于 provider (qwen/openai…) │ 1.0s       │ ——                  │ 60s   / 10min
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  int pause = 本地配额窗口触顶时的 sleep（对方未拒绝，自家保守降速 —— 短，撑过当前窗口即可）
  ext pause = 对方真返了 429/30900/403 等拒绝信号时的 sleep（长，等服务器消气）

  一般规律 int < ext —— 服务器明确拒绝比"我们自己保守"更严重，要等更久。
  MDB List 比较特殊：external=1d，因为它是 1000 req/day 硬限，撞顶必须等次日 reset

注解（容易踩坑的几条）：

* **assrt 一个命中 video = 3 次请求**：search / detail / dl.assrt.net。三者
  都过 quota_guard.acquire('assrt') 计数（fetch_archive 也加了钩子，否则
  服务器侧每分钟 ~15 req 但自家以为 ~10 req → 撞 30900）。

* **OpenSubtitles 免费 20 下载 / 24h**：撞顶时 /download 返回 HTTP 406
  （**不是 429**）+ body 含 remaining=0 / reset_time_utc。
  download() 解析后调 quota_guard.pause_for() 设精确恢复时间，整源暂停到次日。

* **MDB List 1000 / day**：external_pause = 1 day（24h），因为撞 429 等价于"今日额度用光"，
  必须等次日 reset；internal_pause = 3min（本地小窗口短退避就够）。

* **quota_guard 计数**：batch=True/False 都记 timestamp，确保 UI 单测和
  worker 共享同一个 60s 滑窗 —— 避免自家算 ok 但服务器已撞顶。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# 全局限速常量
# 所有外部服务的 delay / 配额 / 冷却参数统一在此定义（不走 config.yaml）
# 改完重启即生效
#
# 命名约定（重要）：
#   *_DELAY           hard delay：每次请求之间的最小间隔（客户端 client 主动 sleep）
#   *_BATCH_QUOTA     (per_min, per_hour, per_day) 本地滑动窗配额
#   *_INTERNAL_PAUSE  本地配额窗口触顶时主动 sleep（对方未拒绝，自家保守降速 → 短）
#   *_EXTERNAL_PAUSE  对方真返了 429/30900/403 等拒绝信号时 sleep（服务器明确拒绝 → 长）
#
# 一般规律 internal < external —— 服务器拒绝更严重，要给更长冷却时间
# ============================================================================

# ── TMDB ── 官方 ~50 req/10s，硬限制设 0.5s 留 buffer
TMDB_DELAY: float = 0.5
TMDB_BATCH_QUOTA: Tuple[int, int, int] = (30, 900, 5000)   # (min, hour, day)
TMDB_INTERNAL_PAUSE: float = 60.0
TMDB_EXTERNAL_PAUSE: float = 900.0        # 15 min

# ── assrt (字幕) ── 官方 20 req/min（token+IP）
# 实测 4.5s hard delay 仍偶尔撞 30900：服务器侧每个命中 video 实际有
# 3 次请求（search / detail / dl.assrt.net），过去 dl 不计入 quota_guard
# 导致自家算 2 次 / 服务器算 3 次错位。fetch_archive 已加入计数后改 6s。
# - hard delay 6s = 10 req/min（官方限制 50% buffer）
# - batch_per_min=10 是 burst 兜底（hard delay 已经 cap 在 10/min，正常
#   顺序跑触发不到这条，只有多线程异常 burst 才会撞）
ASSRT_DELAY: float = 6.0
ASSRT_BATCH_QUOTA: Tuple[int, int, int] = (10, 500, 2500)
ASSRT_INTERNAL_PAUSE: float = 120.0
ASSRT_EXTERNAL_PAUSE: float = 1800.0      # 30 min

# ── OpenSubtitles (字幕) ── 免费 5 req/10s
OPENSUBTITLES_DELAY: float = 3.0
OPENSUBTITLES_BATCH_QUOTA: Tuple[int, int, int] = (12, 600, 3000)
OPENSUBTITLES_INTERNAL_PAUSE: float = 120.0
OPENSUBTITLES_EXTERNAL_PAUSE: float = 1800.0  # 30 min

# ── Shooter (字幕) ── 无公开限速，礼貌间隔
SHOOTER_DELAY: float = 3.0
SHOOTER_BATCH_QUOTA: Tuple[int, int, int] = (10, 500, 2500)
SHOOTER_INTERNAL_PAUSE: float = 120.0
SHOOTER_EXTERNAL_PAUSE: float = 1800.0    # 30 min

# ── MDB List (评分) ── 1000 req/day，配额耗尽暂停 1 天
MDBLIST_DELAY: float = 1.0
MDBLIST_BATCH_QUOTA: Tuple[int, int, int] = (20, 300, 1000)
MDBLIST_INTERNAL_PAUSE: float = 180.0      # 3 min
MDBLIST_EXTERNAL_PAUSE: float = 86400.0    # 1 day

# ── Douban (评分/片单，纯爬虫) ──
DOUBAN_DELAY: float = 5.0
DOUBAN_BATCH_QUOTA: Tuple[int, int, int] = (10, 300, 2000)
DOUBAN_INTERNAL_PAUSE: float = 180.0       # 3 min
DOUBAN_EXTERNAL_PAUSE: float = 7200.0      # 2 h（被 403 后 IP 通常需要长冷却）

# ── Wikidata (演员图兜底) ──
WIKIDATA_DELAY: float = 1.0
WIKIDATA_BATCH_QUOTA: Tuple[int, int, int] = (15, 600, 3000)
WIKIDATA_INTERNAL_PAUSE: float = 60.0
WIKIDATA_EXTERNAL_PAUSE: float = 1800.0   # 30 min

# ── Trakt (推荐源) ── 无配额
TRAKT_DELAY: float = 1.0
TRAKT_INTERNAL_PAUSE: float = 60.0
TRAKT_EXTERNAL_PAUSE: float = 900.0       # 15 min

# ── AniList (推荐源) ── 官方 90 req/min；只用 hard delay 控
ANILIST_DELAY: float = 1.0
ANILIST_INTERNAL_PAUSE: float = 120.0
ANILIST_EXTERNAL_PAUSE: float = 1800.0    # 30 min

# ── 成人内容刮削 ──
ADULT_SCRAPER_DELAY: float = 3.0
ADULT_SCRAPER_BATCH_QUOTA: Tuple[int, int, int] = (15, 600, 3000)
ADULT_SCRAPER_INTERNAL_PAUSE: float = 180.0
ADULT_SCRAPER_EXTERNAL_PAUSE: float = 1800.0  # 30 min

# ── LLM (qwen / deepseek / 本地) ── 限速取决于供应商，这里给个保底
LLM_DELAY: float = 1.0
LLM_INTERNAL_PAUSE: float = 60.0
LLM_EXTERNAL_PAUSE: float = 600.0         # 10 min

# ============================================================================
# 源配置定义
# ============================================================================

@dataclass
class SourceConfig:
    """单源配额保护配置。"""

    # 硬限制下 client 自身 delay 仍是主控；本字段仅供文档/前端读取
    hard_delay: float = 1.0

    # 外部限流 sleep 秒数：对方返了 429/30900/403 等真实拒绝信号时触发
    external_pause_seconds: float = 60.0

    # batch 调用专属：分钟 / 小时 / 日 配额（0 = 不限）
    batch_per_min: int = 0
    batch_per_hour: int = 0
    batch_per_day: int = 0

    # 本地配额触顶 sleep 秒数：batch_quota 窗口满时主动 sleep（对方未拒绝）
    # 0 = 等同于 external_pause_seconds
    internal_pause_seconds: float = 0.0

    # 描述（日志/前端用）
    description: str = ''

    def effective_internal_pause(self) -> float:
        """本地配额触顶时实际 sleep 秒数 —— 没显式设就 fallback external_pause_seconds。"""
        return self.internal_pause_seconds or self.external_pause_seconds


# 全局源注册表
SOURCE_CONFIGS: Dict[str, SourceConfig] = {
    'tmdb': SourceConfig(
        hard_delay=TMDB_DELAY,
        external_pause_seconds=TMDB_EXTERNAL_PAUSE,
        batch_per_min=TMDB_BATCH_QUOTA[0],
        batch_per_hour=TMDB_BATCH_QUOTA[1],
        batch_per_day=TMDB_BATCH_QUOTA[2],
        internal_pause_seconds=TMDB_INTERNAL_PAUSE,
        description='TMDB (~50 req/10s, HTTP 429)',
    ),
    'assrt': SourceConfig(
        hard_delay=ASSRT_DELAY,
        external_pause_seconds=ASSRT_EXTERNAL_PAUSE,
        batch_per_min=ASSRT_BATCH_QUOTA[0],
        batch_per_hour=ASSRT_BATCH_QUOTA[1],
        batch_per_day=ASSRT_BATCH_QUOTA[2],
        internal_pause_seconds=ASSRT_INTERNAL_PAUSE,
        description='assrt.net (20 req/min, code 30900)',
    ),
    'opensubtitles': SourceConfig(
        hard_delay=OPENSUBTITLES_DELAY,
        external_pause_seconds=OPENSUBTITLES_EXTERNAL_PAUSE,
        batch_per_min=OPENSUBTITLES_BATCH_QUOTA[0],
        batch_per_hour=OPENSUBTITLES_BATCH_QUOTA[1],
        batch_per_day=OPENSUBTITLES_BATCH_QUOTA[2],
        internal_pause_seconds=OPENSUBTITLES_INTERNAL_PAUSE,
        description='OpenSubtitles (search 5req/10s + download 20/24h 免费版, 撞顶 HTTP 406)',
    ),
    'shooter': SourceConfig(
        hard_delay=SHOOTER_DELAY,
        external_pause_seconds=SHOOTER_EXTERNAL_PAUSE,
        batch_per_min=SHOOTER_BATCH_QUOTA[0],
        batch_per_hour=SHOOTER_BATCH_QUOTA[1],
        batch_per_day=SHOOTER_BATCH_QUOTA[2],
        internal_pause_seconds=SHOOTER_INTERNAL_PAUSE,
        description='Shooter (无公开限制, hash 协议)',
    ),
    'mdblist': SourceConfig(
        hard_delay=MDBLIST_DELAY,
        external_pause_seconds=MDBLIST_EXTERNAL_PAUSE,
        batch_per_min=MDBLIST_BATCH_QUOTA[0],
        batch_per_hour=MDBLIST_BATCH_QUOTA[1],
        batch_per_day=MDBLIST_BATCH_QUOTA[2],
        internal_pause_seconds=MDBLIST_INTERNAL_PAUSE,
        description='MDB List (1000 req/day, HTTP 429)',
    ),
    'douban': SourceConfig(
        hard_delay=DOUBAN_DELAY,
        external_pause_seconds=DOUBAN_EXTERNAL_PAUSE,
        batch_per_min=DOUBAN_BATCH_QUOTA[0],
        batch_per_hour=DOUBAN_BATCH_QUOTA[1],
        batch_per_day=DOUBAN_BATCH_QUOTA[2],
        internal_pause_seconds=DOUBAN_INTERNAL_PAUSE,
        description='豆瓣 (无公开 API, 403/429/503 反爬)',
    ),
    'trakt': SourceConfig(
        hard_delay=TRAKT_DELAY,
        external_pause_seconds=TRAKT_EXTERNAL_PAUSE,
        internal_pause_seconds=TRAKT_INTERNAL_PAUSE,
        description='Trakt (rate limit unspecified)',
    ),
    'anilist': SourceConfig(
        hard_delay=ANILIST_DELAY,
        external_pause_seconds=ANILIST_EXTERNAL_PAUSE,
        internal_pause_seconds=ANILIST_INTERNAL_PAUSE,
        description='AniList (90 req/min)',
    ),
    'wikidata': SourceConfig(
        hard_delay=WIKIDATA_DELAY,
        external_pause_seconds=WIKIDATA_EXTERNAL_PAUSE,
        batch_per_min=WIKIDATA_BATCH_QUOTA[0],
        batch_per_hour=WIKIDATA_BATCH_QUOTA[1],
        batch_per_day=WIKIDATA_BATCH_QUOTA[2],
        internal_pause_seconds=WIKIDATA_INTERNAL_PAUSE,
        description='Wikidata SPARQL',
    ),
    'adult': SourceConfig(
        hard_delay=ADULT_SCRAPER_DELAY,
        external_pause_seconds=ADULT_SCRAPER_EXTERNAL_PAUSE,
        batch_per_min=ADULT_SCRAPER_BATCH_QUOTA[0],
        batch_per_hour=ADULT_SCRAPER_BATCH_QUOTA[1],
        batch_per_day=ADULT_SCRAPER_BATCH_QUOTA[2],
        internal_pause_seconds=ADULT_SCRAPER_INTERNAL_PAUSE,
        description='成人内容刮削 (JavBus / JavDB 等)',
    ),
    'llm': SourceConfig(
        hard_delay=LLM_DELAY,
        external_pause_seconds=LLM_EXTERNAL_PAUSE,
        internal_pause_seconds=LLM_INTERNAL_PAUSE,
        description='LLM Provider (配额取决于提供商)',
    ),
}


# ============================================================================
# 源运行时状态
# ============================================================================

@dataclass
class _SourceState:
    """单源运行时状态（由 QuotaGuard 内部管理）。"""
    consecutive_hits: int = 0
    paused_until: float = 0.0
    total_hits: int = 0
    last_hit_at: float = 0.0
    # batch 调用滑动窗时间戳（保留最近 1 天内的）
    batch_timestamps: Deque[float] = field(default_factory=deque)
    # 暂停原因：'external' = 真被对方限了（429/30900/403...）；'preventive' = 本地配额触顶提前暂停
    # —— 给前端 UI 区分用，用户能立刻看出"对方在限我"还是"我们自己保守保护"
    pause_reason: str = ''


# ============================================================================
# QuotaGuard 核心
# ============================================================================

class QuotaGuard:
    """
    进程全局配额守卫。

    核心 API：
      - report_limited(source)  → 上报一次限流事件，返回暂停秒数
      - report_success(source)  → 上报一次成功，重置连续计数
      - acquire(source, batch)  → 请求前检查；阻塞模式下被暂停 / 撞配额会 sleep
      - is_paused(source)       → 非阻塞查询
      - status(source)          → 当前状态摘要
    """

    _DAY_SEC = 86400.0
    _HOUR_SEC = 3600.0
    _MIN_SEC = 60.0

    # 长暂停（>= 10min）才持久化到 kv_cache。短暂停（60-300s 这种）
    # 重启时基本已过期，落库性价比低
    _PERSIST_THRESHOLD_SEC = 600
    _PERSIST_SCOPE = 'quota_guard_paused'

    def __init__(self):
        self._lock = threading.Lock()
        self._states: Dict[str, _SourceState] = {}
        self._restored = False

    # ---- 持久化（lazy，避免 common → web 反向 import 循环）----

    @classmethod
    def _persist_save(cls, source: str, paused_until: float):
        """把 paused_until 落 kv_cache。失败静默 —— 持久化是锦上添花，不能拖累主流程。"""
        if paused_until - time.time() < cls._PERSIST_THRESHOLD_SEC:
            return  # 短暂停不存
        try:
            from backend.cache_store import set_cached
            set_cached(cls._PERSIST_SCOPE, source, {'paused_until': paused_until})
        except Exception:
            pass

    @classmethod
    def _persist_clear(cls, source: str):
        try:
            from backend.cache_store import invalidate
            invalidate(cls._PERSIST_SCOPE, source)
        except Exception:
            pass

    def _restore_once(self):
        """启动后第一次 acquire/report/status 时尝试从 kv_cache 恢复历史暂停。
        过期的项顺手清掉，避免越攒越多。"""
        if self._restored:
            return
        self._restored = True
        try:
            # 读全部 source 在 kv_cache 里的记录（按已知源逐个 get）
            from backend.cache_store import get_cached, invalidate
            now = time.time()
            for source in SOURCE_CONFIGS:
                data = get_cached(
                    self._PERSIST_SCOPE, source,
                    ttl_seconds=365 * self._DAY_SEC, allow_stale=True,
                )
                if not isinstance(data, dict):
                    continue
                until = data.get('paused_until')
                if not isinstance(until, (int, float)):
                    continue
                if until <= now:
                    invalidate(self._PERSIST_SCOPE, source)
                    continue
                state = self._get_state(source)
                state.paused_until = float(until)
                logger.info(
                    f"[QuotaGuard] ⤴ 从 kv_cache 恢复 {source} 暂停状态："
                    f"剩余 {until - now:.0f}s"
                )
        except Exception as e:
            logger.debug(f"[QuotaGuard] 持久化恢复失败（忽略）: {e}")

    def _get_state(self, source: str) -> _SourceState:
        if source not in self._states:
            self._states[source] = _SourceState()
        return self._states[source]

    def _get_config(self, source: str) -> SourceConfig:
        return SOURCE_CONFIGS.get(source, SourceConfig(description=source))

    # ---- 内部辅助 ----

    @staticmethod
    def _has_batch_quota(cfg: SourceConfig) -> bool:
        return cfg.batch_per_min > 0 or cfg.batch_per_hour > 0 or cfg.batch_per_day > 0

    def _prune_timestamps(self, state: _SourceState, now: float):
        cutoff = now - self._DAY_SEC
        ts = state.batch_timestamps
        while ts and ts[0] < cutoff:
            ts.popleft()

    @classmethod
    def _check_batch_quota(cls, state: _SourceState, cfg: SourceConfig,
                           now: float) -> List[str]:
        """返回所有触发的配额标签；空列表 = 未触发。"""
        hits: List[str] = []
        ts = state.batch_timestamps
        if not ts:
            return hits

        if cfg.batch_per_min > 0:
            cnt = sum(1 for t in ts if t > now - cls._MIN_SEC)
            if cnt >= cfg.batch_per_min:
                hits.append(f'min={cnt}/{cfg.batch_per_min}')

        if cfg.batch_per_hour > 0:
            cnt = sum(1 for t in ts if t > now - cls._HOUR_SEC)
            if cnt >= cfg.batch_per_hour:
                hits.append(f'hour={cnt}/{cfg.batch_per_hour}')

        if cfg.batch_per_day > 0:
            cnt = len(ts)
            if cnt >= cfg.batch_per_day:
                hits.append(f'day={cnt}/{cfg.batch_per_day}')

        return hits

    # ---- 上报限流 ----

    def report_limited(self, source: str, reason: str = '') -> float:
        """
        外部源返回限流信号时调用。

        返回：本次暂停的秒数（调用方可据此决定是否继续重试或放弃）。
        """
        with self._lock:
            state = self._get_state(source)
            cfg = self._get_config(source)
            now = time.time()

            state.consecutive_hits += 1
            state.total_hits += 1
            state.last_hit_at = now

            pause_sec = cfg.external_pause_seconds
            state.paused_until = now + pause_sec
            state.pause_reason = 'external'  # 真被对方 429/30900/403... 拒绝
            logger.warning(
                f"[QuotaGuard] ⚠️ {source} 限流 (#{state.consecutive_hits})"
                f" → 暂停 {pause_sec:.0f}s ({cfg.description})"
                f"{f' [{reason}]' if reason else ''}"
            )
        # 锁外持久化（避免 DB 写慢拖累其他 acquire）
        self._persist_save(source, state.paused_until)
        return pause_sec

    # ---- 上报成功 ----

    def report_success(self, source: str):
        """源请求成功时调用，重置连续限流计数。"""
        with self._lock:
            state = self._get_state(source)
            if state.consecutive_hits > 0:
                prev = state.consecutive_hits
                state.consecutive_hits = 0
                if prev >= 2:
                    logger.info(
                        f"[QuotaGuard] ✅ {source} 恢复正常"
                        f"（此前连续 {prev} 次限流）"
                    )

    # ---- 主动等待 ----

    def acquire(self, source: str, batch: bool = False,
                blocking: bool = True) -> float:
        """
        请求前调用：
          - 若该源处于暂停/熔断 → sleep 到解除（blocking=True）或直接返回剩余等待秒数
          - 若 batch=True 且撞到 batch 配额 → 暂停 internal_pause_seconds 再返回
          - 否则记录此次请求时间戳（batch=True 时计入配额窗口）

        返回值：本次实际等待的秒数（无需等待则为 0）。
        """
        # 启动后第一次调用时从持久化恢复历史长暂停（懒加载，零热路径开销）
        self._restore_once()

        waited = 0.0

        # ① 等待已有暂停。分段 sleep（每段最多 5s）每段醒来重读 state，让
        # quota_guard.reset() / 服务器端外部恢复 能在 ≤ 5s 内被感知 ——
        # Python time.sleep() 不可中断，一次性长 sleep 会让 worker 死睡到原 deadline，
        # 即使用户在 UI 手动重置也唤不醒。
        with self._lock:
            state = self._get_state(source)
            pause_left = max(0.0, state.paused_until - time.time())

        if pause_left > 0:
            if not blocking:
                return pause_left
            logger.debug(f"[QuotaGuard] {source} 暂停中，等待 {pause_left:.1f}s ...")
            while True:
                with self._lock:
                    state = self._get_state(source)
                    pause_left = max(0.0, state.paused_until - time.time())
                if pause_left <= 0:
                    break  # 已被 reset 或自然到期
                chunk = min(pause_left, 5.0)
                time.sleep(chunk)
                waited += chunk

        # ② batch 配额检查（只在 batch=True 时启用配额暂停）
        # 关键：检查窗口看的是"所有"请求时间戳（不只是 batch 调用的）。否则
        # 当批量任务和 UI 单测交叉跑时，quota_guard 看不到非 batch 的历史调用，
        # 自家配额未触发但服务器端已超 → 撞 429/限流码（assrt 30900 就是这场景）
        if batch:
            quota_pause = 0.0
            with self._lock:
                state = self._get_state(source)
                cfg = self._get_config(source)
                if self._has_batch_quota(cfg):
                    now = time.time()
                    self._prune_timestamps(state, now)
                    hits = self._check_batch_quota(state, cfg, now)
                    if hits:
                        quota_pause = cfg.effective_internal_pause()
                        state.paused_until = now + quota_pause
                        state.pause_reason = 'internal'  # 本地配额预暂停，对方还没拒绝
                        logger.warning(
                            f"[QuotaGuard] 📊 {source} 批量配额耗尽 "
                            f"[{', '.join(hits)}] → 暂停 {quota_pause:.0f}s（本地预暂停，对方未拒绝）"
                        )

            if quota_pause > 0:
                # 落库（持久化）
                self._persist_save(source, time.time() + quota_pause)
                if not blocking:
                    return quota_pause
                # 同 ① —— 分段 sleep 让 reset 可感知
                while True:
                    with self._lock:
                        state = self._get_state(source)
                        pause_left = max(0.0, state.paused_until - time.time())
                    if pause_left <= 0:
                        break
                    chunk = min(pause_left, 5.0)
                    time.sleep(chunk)
                    waited += chunk

        # ③ 记录请求时间戳（不论 batch / non-batch 都记，对齐服务器侧滑窗）
        with self._lock:
            state = self._get_state(source)
            state.batch_timestamps.append(time.time())

        return waited

    # ---- 查询 ----

    def is_paused(self, source: str) -> bool:
        with self._lock:
            state = self._get_state(source)
            return state.paused_until > time.time()

    def status(self, source: str) -> Dict:
        # 首次访问 status 时尝试从 kv_cache 恢复历史暂停（懒加载）
        self._restore_once()
        with self._lock:
            state = self._get_state(source)
            cfg = self._get_config(source)
            now = time.time()
            paused_remaining = max(0.0, state.paused_until - now)

            # batch 窗口实时计数（不修改 state）
            ts = state.batch_timestamps
            batch_min = sum(1 for t in ts if t > now - self._MIN_SEC) if cfg.batch_per_min else 0
            batch_hour = sum(1 for t in ts if t > now - self._HOUR_SEC) if cfg.batch_per_hour else 0
            batch_day = len(ts) if cfg.batch_per_day else 0

            return {
                'source': source,
                'description': cfg.description,
                'is_paused': paused_remaining > 0,
                # 'external' = 真被对方限了（429/30900/403...）；'preventive' = 本地配额触顶提前暂停（对方未拒绝）
                # 没被暂停过则空串。前端按此分两档展示，避免用户误以为对方在限自己
                'pause_reason': state.pause_reason if paused_remaining > 0 else '',
                # 绝对恢复时间戳（unix 秒）—— 前端用这个显示具体恢复时间点，避免倒计时不自动刷新
                'paused_until_ts': int(state.paused_until) if paused_remaining > 0 else None,
                # 保留剩余秒数字段，供后端日志/调试用；前端不再展示
                'paused_remaining_sec': round(paused_remaining, 1),
                'consecutive_hits': state.consecutive_hits,
                'total_hits': state.total_hits,
                'last_hit_at': state.last_hit_at or None,
                'hard_delay': cfg.hard_delay,
                'batch_quota': {
                    'per_min': {'used': batch_min, 'limit': cfg.batch_per_min},
                    'per_hour': {'used': batch_hour, 'limit': cfg.batch_per_hour},
                    'per_day': {'used': batch_day, 'limit': cfg.batch_per_day},
                },
            }

    def all_status(self) -> Dict[str, Dict]:
        return {source: self.status(source) for source in SOURCE_CONFIGS}

    def configure(self, source: str, **kwargs):
        """运行时修改某源的配置参数（极少用，主要给调试 / 测试）。"""
        with self._lock:
            if source not in SOURCE_CONFIGS:
                SOURCE_CONFIGS[source] = SourceConfig()
            cfg = SOURCE_CONFIGS[source]
            for k, v in kwargs.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            logger.info(f"[QuotaGuard] {source} 配置已更新: {kwargs}")

    def pause_for(self, source: str, seconds: float, reason: str = ''):
        """
        外部主动设置某源暂停时长（覆盖默认 external_pause_seconds）。
        适用于"服务器明确告知恢复时间"的场景：
          - OpenSubtitles 24h 下载额度耗尽（HTTP 406 body 含 reset_time）
          - MDB List 1000 req/day 耗尽（next day 才能恢复）
          - assrt token 临时封禁（按 server 给的 retry-after）
        与 report_limited 区别：不递增 consecutive_hits，不触发熔断升级；
        纯粹是"我比你更知道该等多久"的硬覆盖。
        """
        if seconds <= 0:
            return
        with self._lock:
            state = self._get_state(source)
            state.paused_until = max(state.paused_until, time.time() + seconds)
            state.total_hits += 1
            state.last_hit_at = time.time()
            state.pause_reason = 'external'  # 服务器明确给了 retry-after 等信号
            logger.warning(
                f"[QuotaGuard] ⏸ {source} 暂停 {seconds:.0f}s"
                f"{f' [{reason}]' if reason else ''}"
            )
        # 锁外持久化（长暂停才落库；短暂停 < 10min 跳过）
        self._persist_save(source, state.paused_until)

    def reset(self, source: str):
        """手动重置某源状态（管理 / 调试）。同时清掉 kv_cache 持久化记录。"""
        with self._lock:
            if source in self._states:
                self._states[source] = _SourceState()
                logger.info(f"[QuotaGuard] {source} 状态已手动重置")
        self._persist_clear(source)


# ============================================================================
# 进程全局单例
# ============================================================================

quota_guard = QuotaGuard()
