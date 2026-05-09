"""
配置管理 API
读取/写入 config.yaml，支持热加载。
"""
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.backend.config import settings, ROOT_DIR

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_PATH = ROOT_DIR / "config.yaml"
BACKUP_DIR = ROOT_DIR / "data" / "config_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

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
    """写入 config.yaml，先备份原文件。"""
    if CONFIG_PATH.exists():
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = BACKUP_DIR / f"config_{ts}.yaml"
        shutil.copy2(CONFIG_PATH, backup_path)
        # 只保留最近 20 份
        backups = sorted(BACKUP_DIR.glob("config_*.yaml"), reverse=True)
        for old in backups[20:]:
            old.unlink(missing_ok=True)

    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, indent=2)
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

    importlib.reload(config_module)  # 重新执行 module 代码 → _yaml_config 重读 + Settings 重定义 + settings 重建
    new_settings = config_module.settings
    for field_name in new_settings.model_fields.keys():
        try:
            setattr(settings, field_name, getattr(new_settings, field_name))
        except Exception as e:
            logger.warning(f"同步 settings.{field_name} 失败: {e}")

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


@router.get("/config/full")
def get_full_config():
    """获取完整配置（不脱敏，原值返回）"""
    data = _load_yaml()
    return {"config": data}


@router.put("/config/full")
def update_full_config(payload: ConfigPayload):
    """写入完整配置（深度合并：dict 递归合并，其他类型覆盖）"""
    try:
        old = _load_yaml()
        merged = _deep_merge(old, payload.data)
        path = _save_yaml(merged)
        _reload_settings()
        return {
            "saved": True,
            "path": str(path),
            "config": merged,
        }
    except Exception as e:
        logger.exception("写入配置失败")
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")


@router.get("/config/backups")
def list_backups():
    """列出配置备份文件"""
    backups: List[Dict] = []
    for p in sorted(BACKUP_DIR.glob("config_*.yaml"), reverse=True):
        try:
            stat = p.stat()
            backups.append({
                "name": p.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except OSError:
            continue
    return {"backups": backups}
