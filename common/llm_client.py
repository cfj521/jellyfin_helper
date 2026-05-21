"""
LLM 类型识别客户端 — OpenAI compatible 接口（qwen / deepseek / openai / lmstudio 本地）。

Spike 实测：qwen-plus 在 30 个真实种子样本上 100% 准确，¥0.0007/调用。
LM Studio 走 http://localhost:1234/v1 本地端点，api_key 可填任意非空占位值
（如 'lm-studio'），实际不校验。

本模块只做 chat completions 调用 + JSON 输出强制 + 缓存。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from common.rate_limiter import quota_guard
from backend.config import settings
from backend.database import LLMClassifyCache, SessionLocal

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a media classification expert for torrent release names. Given a torrent name (and optionally file list), output a JSON with these fields:

{
  "media_type": "movie" | "tv" | "anime" | "adult" | "unknown",
  "title_native": "原始/native 标题（日语原名 / 中文原名 / 英文原名等）",
  "title_en": "英文标题（如有）",
  "title_zh": "中文标题（如有）",
  "year": 2024 (or null),
  "season": 1 (or null, only for tv/anime),
  "episode": 12 (or null, only for tv/anime if it's a single episode),
  "release_group": "...（最后 - 后面的发布组）",
  "confidence": 0.0-1.0,
  "tmdb_search_hint": "推荐用于 TMDB 搜索的英文/原文标题（最准的搜索词，去掉 release tags / 集号 / 字幕组前缀）",
  "reasoning": "1 句话解释判定依据"
}

判定规则：
- 日漫识别信号：[字幕组名] 前缀（如 [Sakurato] [VCB-Studio] [LoliHouse] [喵萌奶茶屋] [桜都字幕组] [ANi] [DMG] [Nekomoe kissaten]）+ 单集形式 (- 12) 或 [01]，原始日文/罗马音标题。
- 番号识别信号：xxx-NNN 格式（SSIS / STARS / IPX / ABF / FC2-PPV / WAAA 等公司前缀 + 3-7 位数字），通常文件名很短无 release group。
- 美剧识别信号：SxxEyy + release group（如 -NTb -MEMENTO -FLUX）。
- 中文剧识别信号：中文片名 + S0xE0x + release group。
- 电影识别信号：title.year.resolution.source.codec-group 格式，无 SxxExx，可能含 IMAX/UHD/BluRay 等。

只输出 JSON，不要解释，不要 markdown 围栏。"""


class LLMClient:
    """
    OpenAI-compatible chat completions 客户端 + JSON mode + 缓存 + 配额。
    支持 qwen / deepseek / openai / lmstudio 本地 —— 都走 OpenAI 兼容接口。
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or settings.llm

    # ---- 主入口 ----

    def classify_torrent(
        self,
        torrent_name: str,
        files: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        对一个种子的 name + 文件列表做类型识别。
        返回 dict: { media_type, title_native, title_en, title_zh, year, season,
                    episode, release_group, confidence, tmdb_search_hint, reasoning,
                    cached: bool }
        """
        fp = self._fingerprint(torrent_name, files or [])

        # 1. 缓存命中
        cached = self._cache_get(fp)
        if cached:
            cached['cached'] = True
            return cached

        # 2. 真调
        result = self._call_with_retry(torrent_name, files or [])
        result['cached'] = False

        # 3. 落缓存
        self._cache_put(fp, result)

        return result

    # ---- 缓存 ----

    @staticmethod
    def _fingerprint(torrent_name: str, files: List[Dict]) -> str:
        """种子名 + 排序后的文件名列表 → SHA256 指纹（缓存 key）。"""
        names = sorted(f.get('name', '') for f in files if isinstance(f, dict))
        material = (torrent_name or '') + '\n' + '\n'.join(names)
        return hashlib.sha256(material.encode('utf-8')).hexdigest()

    def _cache_get(self, fp: str) -> Optional[Dict]:
        try:
            with SessionLocal() as db:
                row = db.query(LLMClassifyCache).filter_by(fingerprint_hash=fp).first()
                if not row:
                    return None
                # TTL 检查
                if row.created_at:
                    expires_at = row.created_at + timedelta(days=self.cfg.cache_ttl_days)
                    if datetime.utcnow() > expires_at:
                        return None
                return {
                    'media_type': row.media_type,
                    'title_native': row.title_native,
                    'title_en': row.title_en,
                    'title_zh': row.title_zh,
                    'year': row.year,
                    'season': row.season,
                    'episode': row.episode,
                    'confidence': float(row.confidence) if row.confidence is not None else None,
                    'tmdb_search_hint': row.tmdb_search_hint,
                }
        except Exception as e:
            logger.warning(f"LLM cache 读取异常: {e}")
            return None

    def _cache_put(self, fp: str, result: Dict):
        try:
            with SessionLocal() as db:
                row = db.query(LLMClassifyCache).filter_by(fingerprint_hash=fp).first()
                if not row:
                    row = LLMClassifyCache(fingerprint_hash=fp)
                    db.add(row)
                row.media_type = result.get('media_type')
                row.title_native = result.get('title_native')
                row.title_en = result.get('title_en')
                row.title_zh = result.get('title_zh')
                row.year = result.get('year')
                row.season = result.get('season')
                row.episode = result.get('episode')
                row.confidence = result.get('confidence')
                row.tmdb_search_hint = result.get('tmdb_search_hint')
                row.raw_response = json.dumps(result, ensure_ascii=False)[:8000]
                row.created_at = datetime.utcnow()
                db.commit()
        except Exception as e:
            logger.warning(f"LLM cache 写入异常: {e}")

    # ---- LLM 调用 ----

    def _build_user_prompt(self, torrent_name: str, files: List[Dict]) -> str:
        msg = f"Torrent name: {torrent_name}"
        if files:
            file_lines = []
            for f in files[:30]:  # 至多 30 个文件
                name = f.get('name') or ''
                size = f.get('size') or 0
                file_lines.append(f"  {name} ({self._fmt_size(size)})")
            if file_lines:
                msg += f"\n\nFiles ({len(files)} total):\n" + '\n'.join(file_lines)
        msg += "\n\nClassify it and return JSON only."
        return msg

    @staticmethod
    def _fmt_size(b: int) -> str:
        if b < 1024:
            return f'{b}B'
        if b < 1024**2:
            return f'{b/1024:.0f}KB'
        if b < 1024**3:
            return f'{b/1024**2:.0f}MB'
        return f'{b/1024**3:.1f}GB'

    def _call_with_retry(self, torrent_name: str, files: List[Dict]) -> Dict:
        last_err = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return self._call(torrent_name, files)
            except Exception as e:
                last_err = e
                logger.warning(f"LLM call attempt {attempt + 1} 失败: {e}")
                if attempt < self.cfg.max_retries:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"LLM 多次重试仍失败: {last_err}")

    def _call(self, torrent_name: str, files: List[Dict]) -> Dict:
        if not self.cfg.api_key:
            raise ValueError("LLM api_key 未配置")

        payload = {
            'model': self.cfg.model,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': self._build_user_prompt(torrent_name, files)},
            ],
            'response_format': {'type': 'json_object'},
            'temperature': 0.1,
        }

        # 保底层：先检查全局暂停
        quota_guard.acquire('llm')

        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        r = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {self.cfg.api_key}',
                'Content-Type': 'application/json',
            },
            timeout=self.cfg.timeout_seconds,
        )
        if r.status_code == 429:
            quota_guard.report_limited('llm', 'HTTP 429')
            raise RuntimeError("LLM 限流 (429)")
        r.raise_for_status()
        quota_guard.report_success('llm')
        data = r.json()
        content = data['choices'][0]['message']['content']
        result = json.loads(content)

        # 字段标准化（缺失字段填 None）
        for k in ('media_type', 'title_native', 'title_en', 'title_zh',
                  'year', 'season', 'episode', 'release_group',
                  'confidence', 'tmdb_search_hint', 'reasoning'):
            result.setdefault(k, None)
        return result


    # ---- 通用 chat（非种子分类场景：RSS 正则辅助、文本工具等）----

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        timeout: Optional[int] = None,
        disable_reasoning: bool = False,
    ) -> Dict:
        """
        通用 OpenAI-compatible chat → JSON 响应。
        不走 classify_torrent 的缓存（每次调用 prompt 不同，缓存价值低）。

        disable_reasoning=True 时关掉模型的"思考链"（reasoning / thinking 模式），
        简单结构化任务（如生成正则）开了反而慢且贵：
          - DashScope Qwen3 系列：传 enable_thinking=false（OpenAI 兼容模式接受 extra body）
          - DeepSeek：reasoning_effort='none' 或选 deepseek-chat 而非 deepseek-reasoner
          - OpenAI：本参数无影响（GPT 系列没有 thinking 开关）
        给非 Qwen / 非 DeepSeek 的 provider 传这些字段会被服务端 ignore，无副作用。

        失败时抛 RuntimeError；JSON 解析失败抛 ValueError。
        """
        if not self.cfg.api_key:
            raise ValueError("LLM api_key 未配置")

        payload = {
            'model': self.cfg.model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'response_format': {'type': 'json_object'},
            'temperature': temperature,
        }
        if disable_reasoning:
            # Qwen3 OpenAI 兼容模式参数（DashScope 实测有效）
            payload['enable_thinking'] = False
            # DeepSeek-R1 / OpenAI o-series 通用参数
            payload['reasoning_effort'] = 'none'

        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        r = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {self.cfg.api_key}',
                'Content-Type': 'application/json',
            },
            timeout=timeout or self.cfg.timeout_seconds,
        )
        r.raise_for_status()
        data = r.json()
        content = data['choices'][0]['message']['content']
        return json.loads(content)


# 单例
_default_client: Optional[LLMClient] = None


def get_default_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
