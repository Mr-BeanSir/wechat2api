"""会话 id 判定 —— 复刻 D:\\Development\\sub2api 的判定链路。

sub2api 里的判定顺序（见 ``backend/internal/service/openai_gateway_scheduling.go``、
``session_id.go``、``openai_content_session_seed.go``）:

1. 显式请求头（``explicitOpenAIHeaderSessionNames``）:
   ``session-id`` / ``session_id`` / ``conversation_id`` /
   ``X-Session-Affinity`` / ``X-Session-Id`` / ``X-OpenCode-Session`` /
   ``X-Conversation-ID``
2. Claude Code 请求头: ``X-Claude-Code-Session-Id``
3. Grok 专用请求头: ``X-Grok-Conv-Id``
4. 请求体 ``prompt_cache_key``
5. 内容种子（``deriveOpenAIContentSessionSeed``，前缀 ``compat_cs_``）:
   ``model`` + ``tools`` / ``functions`` + ``instructions`` + 首位
   ``system`` / ``developer`` 前缀 + 第一条 ``user`` 消息。
   这些字段一轮对话内稳定，因此同一轮对话每次请求算出同一个种子。

得到 sessionID 之后 sub2api 会转成哈希做粘性路由（当前
``xxhash64`` 的 16 位十六进制，兼容旧的 ``sha256`` 全量十六进制）。
本模块把同样的哈希算法一并实现，用于日志和 Redis 键的稳定性校验。

清洗规则（``sanitizeSessionID``）:
去空白后为空 -> 丢弃；长度 > 255 丢弃；含 ``< 0x20`` 或 ``0x7f`` 的控制字符丢弃；
非法 UTF-8 丢弃（Python str 层已经解码，这里对 bytes 入口做严格解码）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

MAX_PERSISTED_SESSION_ID_LENGTH = 255

#: 与 sub2api 完全一致的显式请求头优先级
EXPLICIT_SESSION_HEADERS: tuple[str, ...] = (
    "session-id",
    "session_id",
    "conversation_id",
    "x-session-affinity",
    "x-session-id",
    "x-opencode-session",
    "x-conversation-id",
)

CLAUDE_CODE_SESSION_HEADER = "x-claude-code-session-id"
GROK_CONV_HEADER = "x-grok-conv-id"

CONTENT_SEED_PREFIX = "compat_cs_"

#: 内容种子会带上系统提示词/首条 user 的全文，直接当会话 id 用会撑爆
#: Redis key 和 HTTP 头（头只能是 latin-1，中文也塞不进去）。
#: 超过这个长度就压成 ``compat_cs_<16 位哈希>``，稳定且短。
MAX_SESSION_ID_LENGTH = 200

#: 种子里的来源标记，便于排查"这个会话 id 到底哪来的"
SOURCE_HEADER = "header"
SOURCE_CLAUDE_HEADER = "claude-header"
SOURCE_GROK_HEADER = "grok-header"
SOURCE_PROMPT_CACHE_KEY = "prompt_cache_key"
SOURCE_CONTENT_SEED = "content-seed"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class ResolvedSession:
    """判定结果。"""

    session_id: str
    source: str
    is_grok: bool = False

    @property
    def hash(self) -> str:
        return session_hash(self.session_id)

    @property
    def legacy_hash(self) -> str:
        return legacy_session_hash(self.session_id)


# ---------------------------------------------------------------------------
# 清洗
# ---------------------------------------------------------------------------

def sanitize_session_id(raw: Any) -> Optional[str]:
    """sub2api ``sanitizeSessionID`` 的等价实现；不合法返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        raw = str(raw)
    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_PERSISTED_SESSION_ID_LENGTH:
        return None
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return None
    return text


def _raw_hash(text: str) -> str:
    """不做长度清洗的哈希（内容种子可能超过 255，走 sanitize 会统一变成 ""）。"""
    try:
        import xxhash

        return format(xxhash.xxh64(text.encode("utf-8")).intdigest(), "016x")
    except Exception:  # xxhash 缺失时退回 sha256 前 16 位
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compact_content_seed(seed: str) -> str:
    """把过长的内容种子压成 ``compat_cs_<16 位哈希>``；短的原样返回。"""
    if not seed:
        return seed
    if len(seed) <= MAX_SESSION_ID_LENGTH:
        return seed
    prefix = CONTENT_SEED_PREFIX if seed.startswith(CONTENT_SEED_PREFIX) else ""
    return f"{prefix}{_raw_hash(seed)}"


def _header_get(headers: Optional[Mapping[str, Any]], name: str) -> Optional[str]:
    if not headers:
        return None
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def explicit_header_session_id(headers: Optional[Mapping[str, Any]]) -> Optional[str]:
    """第 1 步: 显式请求头列表，取第一个清洗后非空的。"""
    for name in EXPLICIT_SESSION_HEADERS:
        found = sanitize_session_id(_header_get(headers, name))
        if found:
            return found
    return None


def claude_code_session_id(headers: Optional[Mapping[str, Any]]) -> Optional[str]:
    return sanitize_session_id(_header_get(headers, CLAUDE_CODE_SESSION_HEADER))


def grok_conv_session_id(headers: Optional[Mapping[str, Any]]) -> Optional[str]:
    return sanitize_session_id(_header_get(headers, GROK_CONV_HEADER))


# ---------------------------------------------------------------------------
# 内容种子
# ---------------------------------------------------------------------------

def _is_grok(model: str, headers: Optional[Mapping[str, Any]]) -> bool:
    """sub2api 只在 Grok 模型上认 ``X-Grok-Conv-Id``。"""
    if "grok" in (model or "").lower():
        return True
    ua = (_header_get(headers, "user-agent") or "").lower()
    return "grok" in ua


def _normalize_seed_value(value: Any) -> str:
    """把种子字段规范化成稳定字符串。

    字符串原样返回；其它结构（tools/instructions/多模态 content）压成
    键序固定的紧凑 JSON，保证同内容每次得到同一结果。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _first_user_or_input(body: Mapping[str, Any]) -> tuple[list[tuple[str, Any]], bool]:
    """返回 (消息列表, 是否有可用锚点)。兼容 chat 的 ``messages`` 与 responses 的 ``input``。"""
    messages = body.get("messages")
    if isinstance(messages, list):
        return [(str((m or {}).get("role", "")).lower(), (m or {}).get("content")) for m in messages if isinstance(m, Mapping)], True
    inp = body.get("input")
    if isinstance(inp, str):
        return [("user", inp)], True
    if isinstance(inp, list):
        out: list[tuple[str, Any]] = []
        for item in inp:
            if isinstance(item, Mapping):
                if str(item.get("type", "")) == "input_text":
                    out.append(("user", item.get("text")))
                else:
                    out.append((str(item.get("role", "")).lower(), item.get("content") or item.get("text")))
        return out, True
    return [], False


def derive_content_session_seed(body: Optional[Mapping[str, Any]]) -> str:
    """第 5 步: 内容种子。没有可用锚点（比如纯 model 请求）返回空串。"""
    if not body:
        return ""
    parts: list[str] = []
    model = body.get("model")
    if isinstance(model, str) and model.strip():
        parts.append(f"|model={model.strip()}")
    for key in ("tools", "functions", "instructions"):
        value = body.get(key)
        if value:
            normalized = _normalize_seed_value(value)
            if normalized:
                parts.append(f"|{key}={normalized}")

    messages, has_anchor = _first_user_or_input(body)
    if not has_anchor:
        return ""

    system_prefix_open = True
    first_user_captured = False
    for role, content in messages:
        if role in ("system", "developer"):
            if system_prefix_open:
                normalized = _normalize_seed_value(content)
                if normalized:
                    parts.append(f"|system={normalized}")
        elif role == "user":
            system_prefix_open = False
            if not first_user_captured:
                normalized = _normalize_seed_value(content)
                if normalized:
                    parts.append(f"|first_user={normalized}")
                first_user_captured = True
        else:
            system_prefix_open = False

    if not first_user_captured:
        return ""  # 没有 user 锚点 -> 不用内容做会话身份
    if not parts:
        return ""
    return CONTENT_SEED_PREFIX + "".join(parts)


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------

def resolve_session_id(
    headers: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
    request_id: str = "",
) -> ResolvedSession:
    """按 sub2api 的优先级算出这次请求的会话 id。"""
    body = body or {}
    model = body.get("model") if isinstance(body.get("model"), str) else ""
    grok = _is_grok(model or "", headers)

    found = explicit_header_session_id(headers)
    if found:
        return ResolvedSession(found, SOURCE_HEADER, grok)

    found = claude_code_session_id(headers)
    if found:
        return ResolvedSession(found, SOURCE_CLAUDE_HEADER, grok)

    if grok:
        found = grok_conv_session_id(headers)
        if found:
            return ResolvedSession(found, SOURCE_GROK_HEADER, grok)

    cache_key = sanitize_session_id(body.get("prompt_cache_key"))
    if cache_key:
        return ResolvedSession(cache_key, SOURCE_PROMPT_CACHE_KEY, grok)

    seed = derive_content_session_seed(body)
    if seed:
        return ResolvedSession(compact_content_seed(seed), SOURCE_CONTENT_SEED, grok)

    # 实在判不出来时退化：用 request_id（或调用方给的其它标记）兜底，
    # 保证每个请求至少有一个独立会话，而不是全部挤在一起。
    fallback = sanitize_session_id(request_id) or "anon"
    return ResolvedSession(fallback, SOURCE_FALLBACK, grok)


# ---------------------------------------------------------------------------
# 哈希（与 sub2api deriveOpenAISessionHashes 对齐）
# ---------------------------------------------------------------------------

def session_hash(session_id: str) -> str:
    """当前算法: xxhash64 -> 16 位十六进制。"""
    return _raw_hash(sanitize_session_id(session_id) or "")


def legacy_session_hash(session_id: str) -> str:
    """旧算法: sha256 全量十六进制。"""
    normalized = sanitize_session_id(session_id) or ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def short_id(session_id: str) -> str:
    """日志用的短标记。"""
    return session_hash(session_id)[:12]


def iter_headers(pairs: Iterable[Sequence[str]]) -> dict[str, str]:
    """把 [(k, v), ...] 变成 dict（同名头后者胜，与多数网关实现保持一致）。"""
    out: dict[str, str] = {}
    for pair in pairs:
        if len(pair) >= 2:
            out[str(pair[0])] = str(pair[1])
    return out


__all__ = [
    "EXPLICIT_SESSION_HEADERS",
    "CLAUDE_CODE_SESSION_HEADER",
    "GROK_CONV_HEADER",
    "CONTENT_SEED_PREFIX",
    "MAX_SESSION_ID_LENGTH",
    "ResolvedSession",
    "sanitize_session_id",
    "compact_content_seed",
    "explicit_header_session_id",
    "claude_code_session_id",
    "grok_conv_session_id",
    "derive_content_session_seed",
    "resolve_session_id",
    "session_hash",
    "legacy_session_hash",
    "short_id",
    "iter_headers",
]
