"""
配置管理 API
读取/写入 config.yaml，支持热加载。
"""
import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.backend.config import settings, ROOT_DIR

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_PATH = ROOT_DIR / "config.yaml"

# 已不再脱敏——根据用户要求，配置页面直接显示原值
# 这意味着 GET /api/config/full 返回的 dict 包含真实的 API Key / 密码
# 注意：仅当 web 前端在受信任网络访问时安全


class ConfigPayload(BaseModel):
    """前端提交的完整配置 dict（嵌套结构同 config.yaml）"""
    data: Dict[str, Any]


def _load_yaml() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_yaml(data: Dict[str, Any]) -> Path:
    """写入 config.yaml（私人仓库，配置由 git 管理，不再本地备份）。"""
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, indent=2)
    logger.info(f"config 写入: {CONFIG_PATH}")
    return CONFIG_PATH


def _deep_merge(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    把前端提交的 new 深度合并进 old：
      - dict 值递归合并
      - 其他类型（含 list）直接覆盖
      - 用户传什么写什么，不再保留所谓"敏感字段"原值
    """
    merged = dict(old)
    for section, new_values in new.items():
        if isinstance(new_values, dict) and isinstance(old.get(section), dict):
            merged[section] = _deep_merge(old[section], new_values)
        else:
            merged[section] = new_values
    return merged


def _reload_settings():
    """
    重新加载 settings：
    Pydantic Settings 类的字段默认值（_yaml_config.get(...)）只在类定义时计算一次。
    所以仅重新赋值 _yaml_config 不够，必须 reload 整个 config 模块让 class 重新定义。
    然后把新 settings 实例的所有字段值同步到全局 settings 对象（保持引用稳定）。
    """
    import importlib
    from web.backend import config as config_module

    logger.info("config 热重载: reload web.backend.config 模块 + 同步 settings 字段")
    importlib.reload(config_module)  # 重新执行 module 代码 → _yaml_config 重读 + Settings 重定义 + settings 重建
    new_settings = config_module.settings
    synced = 0
    for field_name in new_settings.model_fields.keys():
        try:
            setattr(settings, field_name, getattr(new_settings, field_name))
            synced += 1
        except Exception as e:
            logger.warning(f"同步 settings.{field_name} 失败: {e}")
    logger.info(f"config 热重载: 已同步 {synced}/{len(new_settings.model_fields)} 个字段")

    # settings 变更后：
    #   - watcher 比对新旧 library_ids，对新增的库立即触发扫描
    #   - WebSocket 客户端重新评估连接（auto_scrape / library_ids 变化都生效）
    try:
        from web.backend.services.adult_watcher import watcher
        watcher.restart_for_new_libraries()
    except Exception as e:
        logger.warning(f"watcher.restart_for_new_libraries 失败: {e}")

    try:
        from web.backend.services.jellyfin_ws import client as ws_client
        ws_client.notify_settings_changed()
    except Exception as e:
        logger.warning(f"通知 ws client 失败: {e}")

    # 清空 TMDB 列表/详情缓存（避免显示旧语言数据）
    try:
        from web.backend.api.discover import _cache_clear
        _cache_clear()
    except Exception:
        pass


@router.post("/config/check-db")
def check_database_connection():
    """
    检测 PostgreSQL 数据库连接：
      ① 能否连上（host:port 可达 + 认证通过）
      ② 数据库版本
      ③ 表数量（验证 schema 已初始化）
    """
    import time
    from sqlalchemy import text

    host = settings.db_host
    port = settings.db_port
    db_name = settings.db_name

    if not host:
        return {'ok': False, 'message': '未配置数据库 host'}

    t0 = time.time()
    try:
        from web.backend.database import engine
        with engine.connect() as conn:
            # 版本
            row = conn.execute(text("SELECT version()")).fetchone()
            version_str = row[0] if row else '未知'
            # 表数量
            row2 = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )).fetchone()
            table_count = row2[0] if row2 else 0
        elapsed = round((time.time() - t0) * 1000)
        # 截短版本信息（只取第一行，去掉编译细节）
        short_ver = version_str.split(',')[0] if ',' in version_str else version_str
        return {
            'ok': True,
            'message': f'连接正常（{elapsed}ms）',
            'version': short_ver,
            'host': f'{host}:{port}',
            'database': db_name,
            'table_count': table_count,
        }
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        err = str(e)
        # 友好化常见错误
        if 'could not connect' in err or 'Connection refused' in err:
            msg = f'无法连接 {host}:{port}（连接被拒绝）'
        elif 'password authentication failed' in err:
            msg = '认证失败（用户名或密码错误）'
        elif 'does not exist' in err:
            msg = f'数据库 "{db_name}" 不存在'
        else:
            msg = err[:200]
        return {
            'ok': False,
            'message': msg,
            'host': f'{host}:{port}',
            'database': db_name,
        }


@router.get("/config/full")
def get_full_config():
    """获取完整配置（不脱敏，原值返回）"""
    data = _load_yaml()
    return {"config": data}


def _diff_top_sections(old: Dict[str, Any], new_payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """对比顶级 section + 二级 key，返回 {section: [changed_top_keys]}。
    **不记录具体值**（API key / 密码可能在内），只记录"哪个段哪些字段被改了"作审计线索。"""
    changes: Dict[str, List[str]] = {}
    for section, new_val in new_payload.items():
        old_val = old.get(section)
        if isinstance(new_val, dict) and isinstance(old_val, dict):
            changed_keys = [k for k, v in new_val.items() if old_val.get(k) != v]
            # 只记下 key 名，不含 value
            if changed_keys:
                changes[section] = changed_keys
        elif new_val != old_val:
            changes[section] = ['*整段替换']
    return changes


@router.put("/config/full")
def update_full_config(payload: ConfigPayload):
    """写入完整配置（深度合并：dict 递归合并，其他类型覆盖）"""
    try:
        old = _load_yaml()
        # 审计日志：哪些 section / 哪些字段被改了（不打 value，避免 API key 落日志）
        diff = _diff_top_sections(old, payload.data)
        logger.warning(
            f"/config/full PUT: 用户提交 sections={list(payload.data.keys())} "
            f"diff={diff or '无变化'}"
        )
        merged = _deep_merge(old, payload.data)
        path = _save_yaml(merged)
        _reload_settings()
        logger.info(f"/config/full PUT 完成: path={path}")
        return {
            "saved": True,
            "path": str(path),
            "config": merged,
        }
    except Exception as e:
        logger.exception("写入配置失败")
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")


