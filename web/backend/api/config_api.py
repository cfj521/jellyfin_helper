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
    #   - 变更监听客户端重新评估启停（auto_scrape / library_ids 变化都生效）
    try:
        from web.backend.services.adult_watcher import watcher
        watcher.restart_for_new_libraries()
    except Exception as e:
        logger.warning(f"watcher.restart_for_new_libraries 失败: {e}")

    try:
        from web.backend.services.jellyfin_ws import client as poller_client
        poller_client.notify_settings_changed()
    except Exception as e:
        logger.warning(f"通知 jellyfin 变更监听器失败: {e}")

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


