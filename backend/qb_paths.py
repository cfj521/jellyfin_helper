r"""qBittorrent 加种路径解析。

背景：
  - 设置页「入库流水线 → 下载目录」(dispatch.download_path) 是用户眼里的"默认下载目录"。
  - 它同时承担两个用途：
      ① 加种子时作为 qB 的 savepath（本模块）
      ② 后端配额监视的 shutil.disk_usage 路径（tools/dispatch/quota.py）
  - 配置按"后端视角"书写（后端能直接 stat 到），跨机 / 容器部署时
    qB 看到的是另一套路径，所以传给 qB 前必须走一次 path_mappings 反向翻译。
    path_mappings 关闭或没命中规则时原样返回（同机 / 路径一致的部署）。

之所以集中到这里，是因为加种入口有多处（搜索页 / 手动添加 / RSS 单条 / 预览），
不能让每个调用点各自处理，否则又会回到"只有某个入口生效"的老问题。
"""
from __future__ import annotations


def resolve_qb_save_path(explicit: str = '') -> str:
    """解析加种子时传给 qB 的 savepath（qB 视角）。

    优先级：request 显式指定 > 设置页配置的下载目录。
    两者都为空 → 返回 ''，调用方不传 savepath，让 qB 用自己的默认下载目录。

    Args:
        explicit: 请求里显式带的保存路径（后端视角）

    Returns:
        qB 视角的保存路径；都未配置时返回 ''。
    """
    from backend.config import settings
    from backend.path_translator import reverse_translate_path_with_settings

    candidate = (explicit or '').strip() or (settings.qbittorrent_download_path or '').strip()
    if not candidate:
        return ''
    return reverse_translate_path_with_settings(candidate) or candidate
