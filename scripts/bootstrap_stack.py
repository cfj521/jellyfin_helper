#!/usr/bin/env python3
"""
jellyfin-helper docker stack 一次性 bootstrap。

功能：
  1. 预填 qBittorrent 配置（密码 jellyfin_helper、API Key、启用 RSS + 自动下载）
  2. 预填 Jackett 配置（API Key）
  3. 等 Jackett 启动后添加 7 个公开 indexer
  4. 把生成的 API Key 写回 config.yaml

设计原则：
  - **幂等**：跑几次都安全；已生成的 key 不会被覆盖
  - **两阶段**：
      Phase 1（容器未起）：预填 conf 文件 + 回写 config.yaml
      Phase 2（jackett 已起）：加 indexer
  - 单脚本检测两阶段；用户跑两次即可（中间夹一个 `docker compose up -d`）

容器内运行：
  - 工作目录 /workspace，挂载宿主项目根
  - 通过容器网络名 jackett:9117 / qbittorrent:8080 访问
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import yaml

# ============================================================
# 路径与常量
# ============================================================
ROOT = Path(os.environ.get('BOOTSTRAP_ROOT', '/workspace')).resolve()
QB_CONF = ROOT / 'data' / 'qbittorrent' / 'qBittorrent' / 'qBittorrent.conf'
JACKETT_CONF = ROOT / 'data' / 'jackett' / 'Jackett' / 'ServerConfig.json'
CONFIG_YAML = ROOT / 'config.yaml'

QB_INTERNAL_URL = os.environ.get('QB_URL', 'http://qbittorrent:8080')
JACKETT_INTERNAL_URL = os.environ.get('JACKETT_URL', 'http://jackett:9117')

QB_PASSWORD = 'jellyfin_helper'

# 用户要的 7 个 indexer。Jackett 内部 ID 全小写无符号；按 name 兜底匹配。
INDEXER_TARGETS = [
    ('52bt',          '52BT'),
    ('dmhy',          'dmhy'),
    ('onejav',        'OneJAV'),
    ('thepiratebay',  'The Pirate Bay'),
    ('therarbg',      'TheRARBG'),
    ('torrentkitty',  'TorrentKitty'),
    ('yts',           'YTS'),
]


# ============================================================
# 小工具
# ============================================================
def log(msg: str) -> None:
    print(f'[bootstrap] {msg}', flush=True)


def fail(msg: str) -> None:
    print(f'[bootstrap] ERROR: {msg}', file=sys.stderr, flush=True)
    sys.exit(1)


# ============================================================
# qBittorrent 配置预填
# ============================================================
def qb_pbkdf2_hash(password: str) -> str:
    """
    生成 qBittorrent 5.x 的 WebUI\\Password_PBKDF2 字段值。
    格式：@ByteArray(<base64 salt>:<base64 PBKDF2-HMAC-SHA512>)
    qB 用 100000 iter、64 字节输出、16 字节随机 salt。
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), salt, 100000, 64)
    return f'@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()})'


def prep_qbittorrent_conf() -> str:
    """
    预填 qBittorrent.conf；返回 API Key（无论新生成还是已有）。
    qB 启动时读 conf 发现密码 / API key 已设，就不会生成临时密码 / 触发首启向导。
    """
    QB_CONF.parent.mkdir(parents=True, exist_ok=True)

    if QB_CONF.exists():
        text = QB_CONF.read_text(encoding='utf-8')
        m = re.search(r'^WebUI\\APIKey\s*=\s*(\S+)\s*$', text, re.M)
        if m:
            log(f'qBittorrent.conf 已存在，复用 API Key: {m.group(1)[:8]}...')
            return m.group(1)
        log('qBittorrent.conf 已存在但缺 APIKey，追加')
        api_key = secrets.token_urlsafe(32)
        text = text.rstrip() + f'\nWebUI\\APIKey={api_key}\n'
        QB_CONF.write_text(text, encoding='utf-8')
        return api_key

    api_key = secrets.token_urlsafe(32)
    password_field = qb_pbkdf2_hash(QB_PASSWORD)

    # 最小可用配置；其余字段 qB 自己用默认补
    conf = f"""[AutoRun]
enabled=false

[BitTorrent]
Session\\DefaultSavePath=/downloads
Session\\TempPathEnabled=false

[Preferences]
WebUI\\Username=admin
WebUI\\Password_PBKDF2="{password_field}"
WebUI\\APIKey={api_key}
WebUI\\Address=*
WebUI\\Port={int(os.environ.get('QB_WEBUI_PORT', 8080))}
WebUI\\CSRFProtection=true
WebUI\\ClickjackingProtection=true
WebUI\\HostHeaderValidation=false
WebUI\\LocalHostAuth=false
Downloads\\SavePath=/downloads/
General\\Locale=zh

[RSS]
AutoDownloader\\EnableProcessing=true
AutoDownloader\\DownloadRepacks=true
Session\\EnableFetching=true
Session\\RefreshInterval=30
"""
    QB_CONF.write_text(conf, encoding='utf-8')
    log(f'已写入 qBittorrent.conf；密码=jellyfin_helper，APIKey={api_key[:8]}...')
    return api_key


# ============================================================
# Jackett 配置预填
# ============================================================
def prep_jackett_conf() -> str:
    """
    预填 Jackett ServerConfig.json；返回 API Key。
    Jackett 首启会自己生成 APIKey 字段，但只要 ServerConfig.json 已存在就尊重已有值。
    """
    JACKETT_CONF.parent.mkdir(parents=True, exist_ok=True)

    if JACKETT_CONF.exists():
        try:
            cfg = json.loads(JACKETT_CONF.read_text(encoding='utf-8'))
            if cfg.get('APIKey'):
                log(f'ServerConfig.json 已存在，复用 API Key: {cfg["APIKey"][:8]}...')
                return cfg['APIKey']
        except Exception as e:
            log(f'ServerConfig.json 解析失败 ({e})，重写')

    api_key = secrets.token_hex(16)  # Jackett API key 通常是 32 字符 hex
    cfg = {
        'Port': 9117,
        'AllowExternal': True,
        'APIKey': api_key,
        'AdminPassword': '',
        'InstanceId': secrets.token_hex(16),
        'BlackholeDir': '',
        'UpdateDisabled': False,
        'UpdatePrerelease': False,
        'BasePathOverride': '',
        'BaseUrlOverride': '',
        'CacheEnabled': True,
        'CacheTtl': 2100,
        'CacheMaxResultsPerIndexer': 1000,
        'FlareSolverrUrl': '',
        'OmdbApiKey': '',
        'OmdbApiUrl': '',
        'ProxyType': 0,
        'ProxyUrl': '',
        'ProxyPort': None,
        'ProxyUsername': '',
        'ProxyPassword': '',
    }
    JACKETT_CONF.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    log(f'已写入 ServerConfig.json；APIKey={api_key[:8]}...')
    return api_key


# ============================================================
# config.yaml 回写
# ============================================================
def update_config_yaml(qb_api_key: str, jackett_api_key: str) -> None:
    if not CONFIG_YAML.exists():
        log(f'config.yaml 不存在；跳过回写。请先 cp config.yaml.example config.yaml')
        return

    raw = CONFIG_YAML.read_text(encoding='utf-8')
    data = yaml.safe_load(raw) or {}

    data.setdefault('qbittorrent', {})
    data['qbittorrent']['host'] = 'http://qbittorrent:8080'
    data['qbittorrent']['api_key'] = qb_api_key

    data.setdefault('jackett', {})
    data['jackett']['url'] = 'http://jackett:9117'
    data['jackett']['api_key'] = jackett_api_key

    # 额外把 database / jellyfin 容器名同步好，省用户手改
    data.setdefault('database', {})
    if data['database'].get('host') in (None, '', '127.0.0.1', 'localhost'):
        data['database']['host'] = 'postgres'
    data.setdefault('jellyfin', {})
    if data['jellyfin'].get('host') in (None, '', 'http://127.0.0.1:8096', 'http://localhost:8096'):
        data['jellyfin']['host'] = 'http://jellyfin:8096'
    # jellyfin SQLite 直读：默认指向同 stack 已挂好的路径
    if not data['jellyfin'].get('db_path'):
        data['jellyfin']['db_path'] = '/jellyfin-data/jellyfin.db'

    # 备份后写
    backup = CONFIG_YAML.with_suffix(f'.yaml.bak.bootstrap.{int(time.time())}')
    backup.write_text(raw, encoding='utf-8')

    CONFIG_YAML.write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False, indent=2),
        encoding='utf-8',
    )
    log(f'已回写 config.yaml（备份 → {backup.name}）')


# ============================================================
# Jackett indexer 添加
# ============================================================
def wait_jackett(api_key: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(
                f'{JACKETT_INTERNAL_URL}/api/v2.0/server/config',
                params={'apikey': api_key},
                timeout=3,
            )
            if r.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def list_jackett_indexers(api_key: str) -> list[dict]:
    r = requests.get(
        f'{JACKETT_INTERNAL_URL}/api/v2.0/indexers',
        params={'apikey': api_key, 'configured': 'false'},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def resolve_indexer_id(available: list[dict], wanted_id: str, wanted_name: str) -> Optional[str]:
    """按 id 精确匹配，失败按 name 模糊匹配。"""
    wid = wanted_id.lower()
    wname = wanted_name.lower()
    for ix in available:
        if ix.get('id', '').lower() == wid:
            return ix['id']
    for ix in available:
        nm = ix.get('name', '').lower()
        if wname == nm or wname in nm or nm in wname:
            return ix['id']
    return None


def add_indexer(api_key: str, indexer_id: str) -> bool:
    """
    Jackett 加 indexer 流程：
      GET  /api/v2.0/indexers/<id>/config  → 拿配置模板（array of fields）
      POST /api/v2.0/indexers/<id>/config  → 提交（公开 indexer 直接回填即可）
    """
    try:
        r = requests.get(
            f'{JACKETT_INTERNAL_URL}/api/v2.0/indexers/{indexer_id}/config',
            params={'apikey': api_key},
            timeout=15,
        )
        if not r.ok:
            log(f'  {indexer_id}: 拿配置模板失败 HTTP {r.status_code}')
            return False
        config_template = r.json()

        r2 = requests.post(
            f'{JACKETT_INTERNAL_URL}/api/v2.0/indexers/{indexer_id}/config',
            params={'apikey': api_key},
            json=config_template,
            timeout=60,
        )
        if r2.ok:
            log(f'  {indexer_id}: ✓ 已添加')
            return True
        log(f'  {indexer_id}: ✗ 提交失败 HTTP {r2.status_code} body={r2.text[:200]}')
        return False
    except requests.RequestException as e:
        log(f'  {indexer_id}: 网络异常 {e}')
        return False


def bootstrap_indexers(api_key: str) -> None:
    log(f'等待 Jackett {JACKETT_INTERNAL_URL} 启动...')
    if not wait_jackett(api_key, timeout=60):
        log('Jackett 60s 内未就绪；indexer 添加阶段跳过')
        log('如果你还没跑 docker compose up -d，那是正常的——up 之后再跑一次本脚本')
        return

    log('Jackett 已就绪，开始添加 indexer：')
    try:
        available = list_jackett_indexers(api_key)
    except Exception as e:
        log(f'拿 Jackett indexer 列表失败：{e}')
        return

    available_ids = {ix.get('id', '').lower() for ix in available}
    log(f'  Jackett 候选 indexer 总数 {len(available)}')

    success, missing = 0, []
    for wid, wname in INDEXER_TARGETS:
        real_id = resolve_indexer_id(available, wid, wname)
        if real_id is None:
            log(f'  {wid} ({wname}): ✗ Jackett 库里没找到——可能 Jackett 版本/名称差异')
            missing.append(wid)
            continue
        if real_id.lower() not in available_ids:
            # 已经被 configure 过（不在 unconfigured 列表里）——跳过
            log(f'  {real_id}: 已配置，跳过')
            success += 1
            continue
        if add_indexer(api_key, real_id):
            success += 1
        else:
            missing.append(real_id)

    log(f'indexer 添加完成：{success}/{len(INDEXER_TARGETS)} 成功')
    if missing:
        log(f'未成功：{missing}；可登 Jackett UI http://localhost:9117 手动 add')


# ============================================================
# 入口
# ============================================================
def main() -> int:
    log(f'ROOT = {ROOT}')

    # ── Phase 1：预填 conf（必须先于 qb/jackett 容器首次启动）──
    qb_key = prep_qbittorrent_conf()
    jackett_key = prep_jackett_conf()
    update_config_yaml(qb_key, jackett_key)

    log('-' * 60)
    log('Phase 1 完成。如果这是首次部署，现在请执行：')
    log('    docker compose up -d')
    log('然后再跑一次本 bootstrap 让它给 Jackett 加 indexer。')
    log('-' * 60)

    # ── Phase 2：加 indexer（jackett 已起）──
    bootstrap_indexers(jackett_key)

    log('-' * 60)
    log('Bootstrap 完成。最后一步：')
    log('    docker compose restart helper      # 让 helper 重读 config.yaml')
    log('-' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
