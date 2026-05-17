"""
集中式配额保护 —— 所有外部 API 请求源统一在此登记限制规则。

设计目标：
  1. 各 client 自身实例级 delay 仍是第一道闸（保持现有 _wait/_rate_limit 不变）
  2. 本模块作为**进程级保底层**：
     - 被动：客户端收到 429/限频信号 → report_limited() 触发暂停
     - 主动：客户端每次请求前 acquire() 检查暂停 / 批量配额
  3. **batch 调用额外受配额约束**（分钟/小时/日滑动窗）
     ── 谁算 batch？UI 批量动作按钮（修海报、修演员…）+ 后台 worker
     ── 普通前台单条请求 batch=False，只受 hard delay + pause 控制
  4. 进程全局单例，线程安全
  5. 日志可观察：触发暂停/熔断/配额耗尽时 WARN；恢复时 INFO

接入方式（两层）：
  A. 被动上报：client 收到 429 / 限频错误后
       quota_guard.report_limited('assrt')
  B. 主动检查：client 每次请求前
       quota_guard.acquire('tmdb', batch=self.batch)
     如果当前已被暂停或撞到 batch 配额则 sleep 后返回等待时长。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
源              │ 硬规则     │ batch 配额          │ pause / batch pause
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TMDB            │ 1req/0.5s  │ 30/min 900/h 5000/d │ 60s  / 15min
assrt           │ 1req/4.5s  │ 10/min 500/h 2500/d │ 120s / 30min
OpenSubtitles   │ 1req/3s    │ 12/min 600/h 3000/d │ 120s / 30min
MDB List        │ 1req/s     │ 20/min 300/h 1000/d │ 1d   / 1d
Douban          │ 1req/5s    │ 10/min 300/h 2000/d │ 180s / 2h
Trakt           │ 1req/s     │ ——                  │ 60s  / ——
AniList         │ 1req/s     │ ——                  │ 60s  / ——
Wikidata        │ 1req/s     │ 15/min 600/h 3000/d │ 60s  / 60s
Shooter         │ 1req/3s    │ 10/min 500/h 2500/d │ 120s / 30min
Adult Scraper   │ 1req/3s    │ 15/min 600/h 3000/d │ 180s / 1h
LLM             │ 1req/s     │ ——                  │ 60s  / ——
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
# ============================================================================

# ── TMDB ── 官方 ~50 req/10s，硬限制设 0.5s 留 buffer
TMDB_DELAY: float = 0.5
TMDB_BATCH_QUOTA: Tuple[int, int, int] = (30, 900, 5000)   # (min, hour, day)
TMDB_PAUSE: float = 60.0
TMDB_BATCH_PAUSE: float = 900.0           # 15 min

# ── assrt (字幕) ── 官方 20 req/min（token+IP），硬限 4.5s 留 buffer
ASSRT_DELAY: float = 4.5
ASSRT_BATCH_QUOTA: Tuple[int, int, int] = (10, 500, 2500)
ASSRT_PAUSE: float = 120.0
ASSRT_BATCH_PAUSE: float = 1800.0         # 30 min

# ── OpenSubtitles (字幕) ── 免费 5 req/10s
OPENSUBTITLES_DELAY: float = 3.0
OPENSUBTITLES_BATCH_QUOTA: Tuple[int, int, int] = (12, 600, 3000)
OPENSUBTITLES_PAUSE: float = 120.0
OPENSUBTITLES_BATCH_PAUSE: float = 1800.0  # 30 min

# ── Shooter (字幕) ── 无公开限速，礼貌间隔
SHOOTER_DELAY: float = 3.0
SHOOTER_BATCH_QUOTA: Tuple[int, int, int] = (10, 500, 2500)
SHOOTER_PAUSE: float = 120.0
SHOOTER_BATCH_PAUSE: float = 1800.0       # 30 min

# ── MDB List (评分) ── 1000 req/day，配额耗尽暂停 1 天
MDBLIST_DELAY: float = 1.0
MDBLIST_BATCH_QUOTA: Tuple[int, int, int] = (20, 300, 1000)
MDBLIST_PAUSE: float = 86400.0            # 1 day
MDBLIST_BATCH_PAUSE: float = 86400.0      # 1 day

# ── Douban (评分/片单，纯爬虫) ──
DOUBAN_DELAY: float = 5.0
DOUBAN_BATCH_QUOTA: Tuple[int, int, int] = (10, 300, 2000)
DOUBAN_PAUSE: float = 180.0
DOUBAN_BATCH_PAUSE: float = 7200.0        # 2 h
DOUBAN_BREAKER_MAX_FAILURES: int = 5      # 连续 N 次失败触发全局熔断
DOUBAN_BREAKER_COOLDOWN: int = 3600       # 熔断冷却（秒）

# ── Wikidata (演员图兜底) ──
WIKIDATA_DELAY: float = 1.0
WIKIDATA_BATCH_QUOTA: Tuple[int, int, int] = (15, 600, 3000)
WIKIDATA_PAUSE: float = 60.0
WIKIDATA_BATCH_PAUSE: float = 60.0        # 与 pause 一致

# ── Trakt (推荐源) ── 无配额
TRAKT_DELAY: float = 1.0
TRAKT_PAUSE: float = 60.0

# ── AniList (推荐源) ── 官方 90 req/min；只用 hard delay 控
ANILIST_DELAY: float = 1.0
ANILIST_PAUSE: float = 60.0

# ── 成人内容刮削 ──
ADULT_SCRAPER_DELAY: float = 3.0
ADULT_SCRAPER_BATCH_QUOTA: Tuple[int, int, int] = (15, 600, 3000)
ADULT_SCRAPER_PAUSE: float = 180.0
ADULT_SCRAPER_BATCH_PAUSE: float = 3600.0  # 1 h

# ── LLM (qwen / deepseek / 本地) ── 限速取决于供应商，这里给个保底
LLM_DELAY: float = 1.0
LLM_PAUSE: float = 60.0

# ============================================================================
# 源配置定义
# ============================================================================

@dataclass
class SourceConfig:
    """单源配额保护配置。"""

    # 硬限制下 client 自身 delay 仍是主控；本字段仅供文档/前端读取
    hard_delay: float = 1.0

    # 触发限流 / 收到 429 后的暂停秒数
    pause_seconds: float = 60.0

    # batch 调用专属：分钟 / 小时 / 日 配额（0 = 不限）
    batch_per_min: int = 0
    batch_per_hour: int = 0
    batch_per_day: int = 0

    # batch 配额耗尽时的暂停秒数（默认与 pause_seconds 等同）
    batch_pause_seconds: float = 0.0

    # 熔断（连续失败 N 次后直接锁死 X 秒）：0 = 不启用
    # 目前只 douban 用，其它源用普通 pause
    circuit_break_after: int = 0
    circuit_break_seconds: float = 3600.0

    # 描述（日志/前端用）
    description: str = ''

    def effective_batch_pause(self) -> float:
        return self.batch_pause_seconds or self.pause_seconds


# 全局源注册表
SOURCE_CONFIGS: Dict[str, SourceConfig] = {
    'tmdb': SourceConfig(
        hard_delay=TMDB_DELAY,
        pause_seconds=TMDB_PAUSE,
        batch_per_min=TMDB_BATCH_QUOTA[0],
        batch_per_hour=TMDB_BATCH_QUOTA[1],
        batch_per_day=TMDB_BATCH_QUOTA[2],
        batch_pause_seconds=TMDB_BATCH_PAUSE,
        description='TMDB (~50 req/10s, HTTP 429)',
    ),
    'assrt': SourceConfig(
        hard_delay=ASSRT_DELAY,
        pause_seconds=ASSRT_PAUSE,
        batch_per_min=ASSRT_BATCH_QUOTA[0],
        batch_per_hour=ASSRT_BATCH_QUOTA[1],
        batch_per_day=ASSRT_BATCH_QUOTA[2],
        batch_pause_seconds=ASSRT_BATCH_PAUSE,
        description='assrt.net (20 req/min, code 30900)',
    ),
    'opensubtitles': SourceConfig(
        hard_delay=OPENSUBTITLES_DELAY,
        pause_seconds=OPENSUBTITLES_PAUSE,
        batch_per_min=OPENSUBTITLES_BATCH_QUOTA[0],
        batch_per_hour=OPENSUBTITLES_BATCH_QUOTA[1],
        batch_per_day=OPENSUBTITLES_BATCH_QUOTA[2],
        batch_pause_seconds=OPENSUBTITLES_BATCH_PAUSE,
        description='OpenSubtitles (5 req/10s free, HTTP 429)',
    ),
    'shooter': SourceConfig(
        hard_delay=SHOOTER_DELAY,
        pause_seconds=SHOOTER_PAUSE,
        batch_per_min=SHOOTER_BATCH_QUOTA[0],
        batch_per_hour=SHOOTER_BATCH_QUOTA[1],
        batch_per_day=SHOOTER_BATCH_QUOTA[2],
        batch_pause_seconds=SHOOTER_BATCH_PAUSE,
        description='Shooter (无公开限制, hash 协议)',
    ),
    'mdblist': SourceConfig(
        hard_delay=MDBLIST_DELAY,
        pause_seconds=MDBLIST_PAUSE,
        batch_per_min=MDBLIST_BATCH_QUOTA[0],
        batch_per_hour=MDBLIST_BATCH_QUOTA[1],
        batch_per_day=MDBLIST_BATCH_QUOTA[2],
        batch_pause_seconds=MDBLIST_BATCH_PAUSE,
        description='MDB List (1000 req/day, HTTP 429)',
    ),
    'douban': SourceConfig(
        hard_delay=DOUBAN_DELAY,
        pause_seconds=DOUBAN_PAUSE,
        batch_per_min=DOUBAN_BATCH_QUOTA[0],
        batch_per_hour=DOUBAN_BATCH_QUOTA[1],
        batch_per_day=DOUBAN_BATCH_QUOTA[2],
        batch_pause_seconds=DOUBAN_BATCH_PAUSE,
        circuit_break_after=DOUBAN_BREAKER_MAX_FAILURES,
        circuit_break_seconds=DOUBAN_BREAKER_COOLDOWN,
        description='豆瓣 (无公开 API, 403/429/503 反爬)',
    ),
    'trakt': SourceConfig(
        hard_delay=TRAKT_DELAY,
        pause_seconds=TRAKT_PAUSE,
        description='Trakt (rate limit unspecified)',
    ),
    'anilist': SourceConfig(
        hard_delay=ANILIST_DELAY,
        pause_seconds=ANILIST_PAUSE,
        description='AniList (90 req/min)',
    ),
    'wikidata': SourceConfig(
        hard_delay=WIKIDATA_DELAY,
        pause_seconds=WIKIDATA_PAUSE,
        batch_per_min=WIKIDATA_BATCH_QUOTA[0],
        batch_per_hour=WIKIDATA_BATCH_QUOTA[1],
        batch_per_day=WIKIDATA_BATCH_QUOTA[2],
        batch_pause_seconds=WIKIDATA_BATCH_PAUSE,
        description='Wikidata SPARQL',
    ),
    'adult': SourceConfig(
        hard_delay=ADULT_SCRAPER_DELAY,
        pause_seconds=ADULT_SCRAPER_PAUSE,
        batch_per_min=ADULT_SCRAPER_BATCH_QUOTA[0],
        batch_per_hour=ADULT_SCRAPER_BATCH_QUOTA[1],
        batch_per_day=ADULT_SCRAPER_BATCH_QUOTA[2],
        batch_pause_seconds=ADULT_SCRAPER_BATCH_PAUSE,
        description='成人内容刮削 (JavBus / JavDB 等)',
    ),
    'llm': SourceConfig(
        hard_delay=LLM_DELAY,
        pause_seconds=LLM_PAUSE,
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
    circuit_open_until: float = 0.0
    total_hits: int = 0
    last_hit_at: float = 0.0
    # batch 调用滑动窗时间戳（保留最近 1 天内的）
    batch_timestamps: Deque[float] = field(default_factory=deque)


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

    def __init__(self):
        self._lock = threading.Lock()
        self._states: Dict[str, _SourceState] = {}

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

            # 熔断判定（目前只 douban 用）
            if cfg.circuit_break_after > 0 and state.consecutive_hits >= cfg.circuit_break_after:
                state.circuit_open_until = now + cfg.circuit_break_seconds
                state.paused_until = state.circuit_open_until
                pause_sec = cfg.circuit_break_seconds
                logger.error(
                    f"[QuotaGuard] 🔴 {source} 熔断：连续 {state.consecutive_hits} 次限流"
                    f" → 暂停 {pause_sec:.0f}s ({cfg.description})"
                    f"{f' [{reason}]' if reason else ''}"
                )
                return pause_sec

            pause_sec = cfg.pause_seconds
            state.paused_until = now + pause_sec
            logger.warning(
                f"[QuotaGuard] ⚠️ {source} 限流 (#{state.consecutive_hits})"
                f" → 暂停 {pause_sec:.0f}s ({cfg.description})"
                f"{f' [{reason}]' if reason else ''}"
            )
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
          - 若 batch=True 且撞到 batch 配额 → 暂停 batch_pause_seconds 再返回
          - 否则记录此次请求时间戳（batch=True 时计入配额窗口）

        返回值：本次实际等待的秒数（无需等待则为 0）。
        """
        waited = 0.0

        # ① 等待已有暂停
        with self._lock:
            state = self._get_state(source)
            pause_left = max(0.0, state.paused_until - time.time())

        if pause_left > 0:
            if not blocking:
                return pause_left
            logger.debug(f"[QuotaGuard] {source} 暂停中，等待 {pause_left:.1f}s ...")
            time.sleep(pause_left)
            waited += pause_left

        # ② batch 配额检查
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
                        quota_pause = cfg.effective_batch_pause()
                        state.paused_until = now + quota_pause
                        logger.warning(
                            f"[QuotaGuard] 📊 {source} 批量配额耗尽 "
                            f"[{', '.join(hits)}] → 暂停 {quota_pause:.0f}s"
                        )

            if quota_pause > 0:
                if not blocking:
                    return quota_pause
                time.sleep(quota_pause)
                waited += quota_pause

            # ③ 记录本次 batch 请求时间戳（落在 acquire 末尾 → 请求即将发出）
            with self._lock:
                state = self._get_state(source)
                state.batch_timestamps.append(time.time())

        return waited

    # ---- 查询 ----

    def is_paused(self, source: str) -> bool:
        with self._lock:
            state = self._get_state(source)
            return state.paused_until > time.time()

    def is_circuit_open(self, source: str) -> bool:
        with self._lock:
            state = self._get_state(source)
            return state.circuit_open_until > time.time()

    def status(self, source: str) -> Dict:
        with self._lock:
            state = self._get_state(source)
            cfg = self._get_config(source)
            now = time.time()
            paused_remaining = max(0.0, state.paused_until - now)
            circuit_remaining = max(0.0, state.circuit_open_until - now)

            # batch 窗口实时计数（不修改 state）
            ts = state.batch_timestamps
            batch_min = sum(1 for t in ts if t > now - self._MIN_SEC) if cfg.batch_per_min else 0
            batch_hour = sum(1 for t in ts if t > now - self._HOUR_SEC) if cfg.batch_per_hour else 0
            batch_day = len(ts) if cfg.batch_per_day else 0

            return {
                'source': source,
                'description': cfg.description,
                'is_paused': paused_remaining > 0,
                'paused_remaining_sec': round(paused_remaining, 1),
                'is_circuit_open': circuit_remaining > 0,
                'circuit_remaining_sec': round(circuit_remaining, 1),
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

    def reset(self, source: str):
        """手动重置某源状态（管理 / 调试）。"""
        with self._lock:
            if source in self._states:
                self._states[source] = _SourceState()
                logger.info(f"[QuotaGuard] {source} 状态已手动重置")


# ============================================================================
# 进程全局单例
# ============================================================================

quota_guard = QuotaGuard()
