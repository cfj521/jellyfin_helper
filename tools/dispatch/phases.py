"""
Dispatch 流水线 phase / status 常量集中地。

设计原则：
  - phase 用进行时（动作正在进行）或完成时（动作已结束）
  - status 反映该 phase 当前的执行结果
  - qB state 映射只在 downloading 阶段使用
"""
from __future__ import annotations

# ============================================================================
# Phase（流水线阶段）—— 字符串值即 DB 存储值
# ============================================================================

# --- 主线 ---
PHASE_ANALYZING                = 'analyzing'                # API push 后，等 metadata + 识别 + 算路径 + 重复检测
PHASE_DISPATCH_QUEUED          = 'dispatch_queued'          # 已确认，等 downloader-watcher 看 qB 进度
PHASE_DOWNLOADING              = 'downloading'              # qB 正在下载
PHASE_DOWNLOAD_DONE            = 'download_done'            # qB progress=100%，等 dispatch-pipeline claim
PHASE_COPYING                  = 'copying'                  # 复制源文件到媒体库
PHASE_ORGANIZING               = 'organizing'               # 按模板改名 + SxxExx 拆 Season + trash 分流
PHASE_JELLYFIN_RECOGNIZING     = 'jellyfin_recognizing'     # 已通知 jellyfin refresh，等 jellyfin-watcher 确认入库
PHASE_JELLYFIN_RECOGNIZE_DONE  = 'jellyfin_recognize_done'  # jellyfin 已认到 item，等 post-process claim
PHASE_SUBTITLE_FETCHING        = 'subtitle_fetching'        # 抓字幕
PHASE_AUDIO_TRACK_ORDER_ADJUSTING = 'audio_track_order_adjusting'  # 调音轨顺序
PHASE_ALL_JOBS_DONE            = 'all_jobs_done'            # 全流程完结，进入做种期

# --- 终态 ---
PHASE_CLEANED                  = 'cleaned'                  # 软清/硬清后
PHASE_DISMISSED                = 'dismissed'                # 用户驳回审核

# 主线推进顺序（用于 sweeper 重派 + UI 进度展示）
PHASE_PIPELINE_ORDER = [
    PHASE_ANALYZING,
    PHASE_DISPATCH_QUEUED,
    PHASE_DOWNLOADING,
    PHASE_DOWNLOAD_DONE,
    PHASE_COPYING,
    PHASE_ORGANIZING,
    PHASE_JELLYFIN_RECOGNIZING,
    PHASE_JELLYFIN_RECOGNIZE_DONE,
    PHASE_SUBTITLE_FETCHING,
    PHASE_AUDIO_TRACK_ORDER_ADJUSTING,
    PHASE_ALL_JOBS_DONE,
]

# 链式段（同一 worker 调用栈一气呵成），用于崩溃恢复扫描
# dispatch-pipeline 链
PIPELINE_CHAIN_PHASES = {
    PHASE_COPYING,
    PHASE_ORGANIZING,
    PHASE_JELLYFIN_RECOGNIZING,
}
# post-process 链
POSTPROCESS_CHAIN_PHASES = {
    PHASE_SUBTITLE_FETCHING,
    PHASE_AUDIO_TRACK_ORDER_ADJUSTING,
}

# 等外部事件段（watcher 自然轮询）
WATCHER_PHASES = {
    PHASE_DOWNLOADING,
    PHASE_JELLYFIN_RECOGNIZING,
}

# 主流水线"在跑"集合 —— quota 硬清不能动这些
ACTIVE_PHASES = (
    PIPELINE_CHAIN_PHASES
    | POSTPROCESS_CHAIN_PHASES
    | WATCHER_PHASES
    | {PHASE_DOWNLOAD_DONE, PHASE_JELLYFIN_RECOGNIZE_DONE}
)

# 已转移完成（quota _cost_score 第一档）
DISPATCHED_PHASES = {PHASE_ALL_JOBS_DONE, PHASE_CLEANED}


# ============================================================================
# Status（每个 phase 的执行结果）
# ============================================================================

STATUS_RUNNING       = 'running'        # 该 phase 正在执行
STATUS_SUCCEEDED     = 'succeeded'      # 该 phase 成功（即将进入下一 phase 或已进入）
STATUS_FAILED        = 'failed'         # 失败 —— sweeper 重派
STATUS_WARNED        = 'warned'         # 非阻断警告（如 jellyfin refresh 调用失败但继续）
STATUS_SKIPPED       = 'skipped'        # 该 phase 因条件不适用跳过
STATUS_NEEDS_REVIEW  = 'needs_review'   # 等用户审核（PHASE_ANALYZING 低置信识别 / PHASE_COPYING 跨种子目标冲突）

# downloading 阶段专属
STATUS_METADATA_PENDING = 'metadata_pending'   # qB metaDL/allocating
STATUS_STALLED          = 'stalled'            # qB stalledDL，无 peer
STATUS_PAUSED           = 'paused'             # qB pausedDL/stoppedDL


# ============================================================================
# qBittorrent state 映射
# ============================================================================

# qB state → downloading 阶段 status
QB_DOWNLOAD_STATE_TO_STATUS = {
    'downloading':  STATUS_RUNNING,
    'forcedDL':     STATUS_RUNNING,
    'queuedDL':     STATUS_RUNNING,        # qB 自己排队，不算我们的错
    'checkingDL':   STATUS_RUNNING,        # 短时态
    'metaDL':       STATUS_METADATA_PENDING,
    'allocating':   STATUS_METADATA_PENDING,
    'stalledDL':    STATUS_STALLED,
    'pausedDL':     STATUS_PAUSED,
    'stoppedDL':    STATUS_PAUSED,         # qB 5.0+ 新增
    'error':        STATUS_FAILED,
    'missingFiles': STATUS_FAILED,
}

# 已下完态 —— 看到这些 state 且 progress>=1.0 才推 download_done
QB_DOWNLOAD_DONE_STATES = {
    'uploading', 'forcedUP', 'queuedUP', 'stalledUP',
    'pausedUP', 'stoppedUP', 'checkingUP',
}

# 短时校验 / 搬迁态 —— 不参与判定，等下次轮询
QB_TRANSIENT_STATES = {
    'checkingDL', 'checkingUP', 'checkingResumeData', 'moving',
}


def map_qb_state_to_download_status(qb_state: str) -> str:
    """qB state → 我们 downloading 阶段的 status（unknown 兜底为 running）。"""
    return QB_DOWNLOAD_STATE_TO_STATUS.get(qb_state or '', STATUS_RUNNING)


def is_qb_download_done(qb_state: str, progress: float) -> bool:
    """progress=1.0 且 state 是已下完态（避开 checking* 短时态）。"""
    if (progress or 0) < 1.0:
        return False
    return (qb_state or '') in QB_DOWNLOAD_DONE_STATES


# ============================================================================
# 旧 phase 兼容映射（迁移期可参考；按 CLAUDE.md dev 阶段策略，建议直接清表）
# ============================================================================

LEGACY_PHASE_MAP = {
    'analyze':   PHASE_ANALYZING,
    'pending':   PHASE_DISPATCH_QUEUED,
    'copy':      PHASE_COPYING,
    'organize':  PHASE_ORGANIZING,
    'jellyfin':  PHASE_JELLYFIN_RECOGNIZING,
    'done':      PHASE_ALL_JOBS_DONE,
    'subtitle':  PHASE_SUBTITLE_FETCHING,
    'audio':     PHASE_AUDIO_TRACK_ORDER_ADJUSTING,
    'cleaned':   PHASE_CLEANED,
    'dismissed': PHASE_DISMISSED,
}

LEGACY_STATUS_MAP = {
    'ok': STATUS_SUCCEEDED,
}
