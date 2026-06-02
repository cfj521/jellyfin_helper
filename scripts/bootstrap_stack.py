#!/usr/bin/env python3
"""
jellyfin-helper docker stack 一次性 bootstrap。

功能：
  1. 预填 qBittorrent 配置（密码 jellyfin_helper、API Key、启用 RSS + 自动下载）
  2. 预填 Jackett 配置（API Key）
  3. 等 Jackett 启动后添加 7 个公开 indexer
  4. 等 Jellyfin 启动后跑 Setup Wizard（admin/jellyfin_helper）+ 申请 API Key
  5. 把所有生成的 API Key 写回 config.yaml

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
JELLYFIN_INTERNAL_URL = os.environ.get('JELLYFIN_URL', 'http://jellyfin:8096')

QB_PASSWORD = 'jellyfin_helper'
JACKETT_PASSWORD = 'jellyfin_helper'
JELLYFIN_USERNAME = 'admin'
JELLYFIN_PASSWORD = 'jellyfin_helper'
JELLYFIN_APIKEY_APP = 'jellyfin-helper'
HELPER_DEFAULT_PASSWORD = 'jellyfin_helper'  # config.yaml auth.users[0].password 默认值

# Jellyfin AuthenticateByName 强制要求 X-Emby-Authorization 标识 client
JELLYFIN_CLIENT_HEADER = (
    'MediaBrowser Client="jellyfin-helper-bootstrap", '
    'Device="bootstrap-script", DeviceId="jh-bootstrap", Version="1.0"'
)

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
def jackett_hash_password(password: str, api_key: str) -> str:
    """
    复现 Jackett SecurityService.HashPassword（C#）：
      var ue = new UnicodeEncoding();        // .NET = UTF-16 LE 无 BOM
      input = password + api_key
      hash = SHA512(ue.GetBytes(input))
      return hex(hash) 小写
    源：https://github.com/Jackett/Jackett/blob/master/src/Jackett.Server/Services/SecurityService.cs
    """
    combined = (password + api_key).encode('utf-16-le')
    return hashlib.sha512(combined).hexdigest()


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
    # AdminPassword 必须用 SHA512(password + api_key) UTF-16 LE 小写 hex
    # 顺序敏感：先生成 api_key 再 hash 密码
    admin_password_hash = jackett_hash_password(JACKETT_PASSWORD, api_key)
    cfg = {
        'Port': 9117,
        'AllowExternal': True,
        'APIKey': api_key,
        'AdminPassword': admin_password_hash,
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
    log(f'已写入 ServerConfig.json；admin 密码={JACKETT_PASSWORD}，APIKey={api_key[:8]}...')
    return api_key


# ============================================================
# config.yaml 回写
# ============================================================
def update_config_yaml(
    qb_api_key: str,
    jackett_api_key: str,
    jellyfin_api_key: Optional[str] = None,
) -> None:
    if not CONFIG_YAML.exists():
        log(f'config.yaml 不存在；跳过回写。请先 cp config.yaml.example config.yaml')
        return

    raw = CONFIG_YAML.read_text(encoding='utf-8')
    data = yaml.safe_load(raw) or {}

    # auth.users[0].password：如果还是 CHANGE_ME / 空，自动设默认 jellyfin_helper
    auth = data.setdefault('auth', {})
    users = auth.setdefault('users', [])
    if users and users[0].get('password') in (None, '', 'CHANGE_ME'):
        users[0]['password'] = HELPER_DEFAULT_PASSWORD
        users[0].setdefault('username', 'admin')
        users[0].setdefault('role', 'admin')
        log(f'auth.users[0] 密码设为默认 {HELPER_DEFAULT_PASSWORD}')

    data.setdefault('qbittorrent', {})
    data['qbittorrent']['host'] = 'http://qbittorrent:8080'
    data['qbittorrent']['api_key'] = qb_api_key

    data.setdefault('jackett', {})
    # backend/config.py 读的字段名是 host（不是 url），写错了 helper 找不到 jackett
    data['jackett']['host'] = 'http://jackett:9117'
    data['jackett']['api_key'] = jackett_api_key

    # 额外把 database / jellyfin 容器名同步好，省用户手改
    data.setdefault('database', {})
    if data['database'].get('host') in (None, '', '127.0.0.1', 'localhost'):
        data['database']['host'] = 'postgres'
    # 密码也一并写：跟 .env 默认 POSTGRES_PASSWORD=jellyfin_helper 对齐
    # 用户如果在 .env 改了别的密码，需手动同步这里
    if data['database'].get('password') in (None, '', 'CHANGE_ME'):
        data['database']['password'] = os.environ.get('POSTGRES_PASSWORD', 'jellyfin_helper')
    data.setdefault('jellyfin', {})
    if data['jellyfin'].get('host') in (None, '', 'http://127.0.0.1:8096', 'http://localhost:8096'):
        data['jellyfin']['host'] = 'http://jellyfin:8096'
    # jellyfin SQLite 直读：默认指向同 stack 已挂好的路径
    if not data['jellyfin'].get('db_path'):
        data['jellyfin']['db_path'] = '/jellyfin-data/jellyfin.db'
    # Jellyfin API Key（phase 2 jellyfin bootstrap 完成后才有；phase 1 调用时 None 跳过）
    if jellyfin_api_key:
        existing = data['jellyfin'].get('api_key', '')
        if existing in (None, '', 'your_jellyfin_api_key'):
            data['jellyfin']['api_key'] = jellyfin_api_key
        elif existing != jellyfin_api_key:
            log(f'config.yaml 已有 jellyfin.api_key（{existing[:8]}...），保留不覆盖')

    # 备份后写
    backup = CONFIG_YAML.with_suffix(f'.yaml.bak.bootstrap.{int(time.time())}')
    backup.write_text(raw, encoding='utf-8')

    CONFIG_YAML.write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False, indent=2),
        encoding='utf-8',
    )
    log(f'已回写 config.yaml（备份 → {backup.name}）')


# ============================================================
# Jellyfin bootstrap：跑 Startup Wizard + 拿 API Key（10.x REST API）
# 端点参考：
#   POST /Startup/Configuration     - 设置 UI/元数据语言（FirstTimeSetup 期间免授权）
#   POST /Startup/User              - 设置首个 admin 用户名 + 密码（同上）
#   POST /Startup/RemoteAccess      - 设置远程访问 / UPnP
#   POST /Startup/Complete          - 标记 wizard 完成
#   POST /Users/AuthenticateByName  - admin 登录拿 AccessToken
#   POST /Auth/Keys?app=<name>      - 创建 API Key（返回 204 NoContent，不带 key 值）
#   GET  /Auth/Keys                 - 列出所有 key，按 AppName 找出刚创建的那条
# ============================================================
def wait_jellyfin(timeout: int = 180) -> Optional[dict]:
    """轮询 /System/Info/Public；返回 system info 或 None 超时。"""
    log(f'等 Jellyfin: {JELLYFIN_INTERNAL_URL}')
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f'{JELLYFIN_INTERNAL_URL}/System/Info/Public', timeout=5)
            if r.ok:
                info = r.json()
                log(f'Jellyfin 已就绪：ver={info.get("Version")}, '
                    f'startup_wizard_done={info.get("StartupWizardCompleted")}')
                return info
        except requests.RequestException:
            pass
        time.sleep(3)
    log('Jellyfin 等待超时（180s）')
    return None


def jellyfin_run_wizard() -> bool:
    """跑 Startup Wizard 4 步。FirstTimeSetup 阶段所有 /Startup/* 端点豁免授权。"""
    base = JELLYFIN_INTERNAL_URL
    steps = [
        ('Configuration', '/Startup/Configuration', {
            'UICulture': 'zh-CN',
            'MetadataCountryCode': 'CN',
            'PreferredMetadataLanguage': 'zh',
        }),
        ('User', '/Startup/User', {
            'Name': JELLYFIN_USERNAME,
            'Password': JELLYFIN_PASSWORD,
        }),
        ('RemoteAccess', '/Startup/RemoteAccess', {
            'EnableRemoteAccess': True,
            'EnableAutomaticPortMapping': False,
        }),
        ('Complete', '/Startup/Complete', None),
    ]
    for name, path, body in steps:
        try:
            r = requests.post(base + path, json=body, timeout=15)
            if not r.ok:
                log(f'Wizard {name} 失败：HTTP {r.status_code} {r.text[:200]}')
                return False
            log(f'Wizard {name} OK')
        except requests.RequestException as e:
            log(f'Wizard {name} 异常：{e}')
            return False
    return True


def jellyfin_login() -> Optional[str]:
    """admin 登录，返回 AccessToken。"""
    try:
        r = requests.post(
            f'{JELLYFIN_INTERNAL_URL}/Users/AuthenticateByName',
            json={'Username': JELLYFIN_USERNAME, 'Pw': JELLYFIN_PASSWORD},
            headers={'X-Emby-Authorization': JELLYFIN_CLIENT_HEADER},
            timeout=15,
        )
    except requests.RequestException as e:
        log(f'登录异常：{e}')
        return None
    if not r.ok:
        log(f'登录失败：HTTP {r.status_code} {r.text[:200]}')
        return None
    token = r.json().get('AccessToken')
    if not token:
        log('登录响应缺 AccessToken')
        return None
    log(f'admin 登录成功，token={token[:8]}...')
    return token


def jellyfin_get_or_create_apikey(token: str) -> Optional[str]:
    """复用已有 AppName=jellyfin-helper 的 key；否则 POST 创建后再 GET 列表取。"""
    base = JELLYFIN_INTERNAL_URL
    auth_header = {'Authorization': f'MediaBrowser Token="{token}"'}

    def list_keys() -> list:
        try:
            r = requests.get(f'{base}/Auth/Keys', headers=auth_header, timeout=10)
        except requests.RequestException as e:
            log(f'GET /Auth/Keys 异常：{e}')
            return []
        if not r.ok:
            log(f'GET /Auth/Keys 失败：HTTP {r.status_code}')
            return []
        return r.json().get('Items', [])

    existing = [k for k in list_keys() if k.get('AppName') == JELLYFIN_APIKEY_APP]
    if existing:
        key = existing[0].get('AccessToken')
        log(f'复用已有 API Key: {key[:8]}...')
        return key

    try:
        r = requests.post(
            f'{base}/Auth/Keys',
            params={'app': JELLYFIN_APIKEY_APP},
            headers=auth_header,
            timeout=10,
        )
    except requests.RequestException as e:
        log(f'POST /Auth/Keys 异常：{e}')
        return None
    if not r.ok:
        log(f'POST /Auth/Keys 失败：HTTP {r.status_code} {r.text[:200]}')
        return None

    # 创建端点返回 204 NoContent 不带 key 值，要再 GET 列表取
    for k in list_keys():
        if k.get('AppName') == JELLYFIN_APIKEY_APP:
            key = k.get('AccessToken')
            log(f'生成 API Key: {key[:8]}...')
            return key
    log('创建后 GET 列表里仍找不到 jellyfin-helper key')
    return None


def bootstrap_jellyfin() -> Optional[str]:
    """完整流程：等 ready → wizard（如未完成）→ 登录 → 拿 api_key。返回 key 或 None。"""
    info = wait_jellyfin()
    if not info:
        return None

    if not info.get('StartupWizardCompleted'):
        log('StartupWizard 未完成，开始向导...')
        if not jellyfin_run_wizard():
            log('向导失败，跳过 API Key 申请')
            return None
        log(f'Wizard 完成。Jellyfin admin = {JELLYFIN_USERNAME} / {JELLYFIN_PASSWORD}')
    else:
        log('StartupWizard 已完成，跳过向导直接拿 API Key')

    token = jellyfin_login()
    if not token:
        return None
    return jellyfin_get_or_create_apikey(token)


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

    # ── Phase 2：加 indexer + Jellyfin wizard + api_key（这一波都要服务已起）──
    bootstrap_indexers(jackett_key)

    log('-' * 60)
    log('Phase 2a 完成。开始 Jellyfin wizard + API Key 申请...')
    jellyfin_key = bootstrap_jellyfin()
    if jellyfin_key:
        # 二次回写：把 jellyfin api_key 也存进去
        update_config_yaml(qb_key, jackett_key, jellyfin_api_key=jellyfin_key)
    else:
        log('Jellyfin bootstrap 未拿到 api_key；helper 仍可启动但 jellyfin 相关功能未就绪。')
        log('排查：docker compose logs jellyfin；手动走 Wizard 后在控制台 → API Keys 自建并填 config.yaml')

    log('-' * 60)
    log('Bootstrap 完成。最后一步：')
    log('    docker compose restart helper      # 让 helper 重读 config.yaml')
    log('-' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
