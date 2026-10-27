"""历史去重 —— 只把"最新内容"转发给微信另一端。

codex / opencode 这类客户端每一轮都会把**完整聊天历史**重新发一遍，
所以不能把整个 ``messages`` 甩给微信，否则对方会看到前面所有内容重复刷屏。

做法:
1. 把收到的 ``messages`` 和 Redis 里存的上一轮历史都规范化成
   ``(role, content文本)`` 序列；
2. 求两者的**最长公共前缀** k —— 前缀相同说明这部分是老历史；
3. 公共前缀为 0 时退一步: 在本次请求里找"上一轮最后一条消息"，
   找到就把锚点之后的内容当作新内容（容忍客户端改写/裁剪了中间几条）；
   都找不到就认为是客户端重写了历史，只取最新一条 user 内容；
4. 新内容里只保留需要转发的角色（``user`` / ``tool`` / ``function``），
   丢掉 ``assistant``（那是上一轮模型的输出，不该再发一遍）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: 需要转发给微信的角色
FORWARD_ROLES = ("user", "tool", "function")

MODE_NEW_SESSION = "new_session"
MODE_PREFIX = "prefix"
MODE_ANCHOR = "anchor"
MODE_REWRITTEN = "rewritten"
MODE_NO_NEW_CONTENT = "no_new_content"
MODE_EMPTY = "empty"


@dataclass
class NormalizedMessage:
    role: str
    text: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.role, self.text)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.text}


@dataclass
class DedupeResult:
    mode: str
    is_new_session: bool
    common_prefix: int
    new_messages: list[NormalizedMessage] = field(default_factory=list)
    new_text: str = ""

    @property
    def has_new_content(self) -> bool:
        return bool(self.new_text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "is_new_session": self.is_new_session,
            "common_prefix": self.common_prefix,
            "new_count": len(self.new_messages),
            "new_text": self.new_text,
        }


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def _content_to_text(content: Any) -> str:
    """把 content 拍平成纯文本（兼容多模态 parts 数组）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        try:
            return bytes(content).decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, Mapping):
                ptype = str(part.get("type", ""))
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
                elif ptype in ("image_url", "input_image"):
                    chunks.append("[图片]")
                elif ptype in ("input_file", "file"):
                    chunks.append("[文件]")
                elif isinstance(part.get("content"), str):
                    chunks.append(part["content"])
        return "\n".join(c for c in chunks if c)
    if isinstance(content, Mapping):
        for key in ("text", "content", "input"):
            if isinstance(content.get(key), str):
                return content[key]
        return ""
    return str(content)


def normalize_message(message: Any) -> NormalizedMessage:
    if isinstance(message, NormalizedMessage):
        return message
    if not isinstance(message, Mapping):
        return NormalizedMessage("user", str(message), {})
    role = str(message.get("role", "") or "").strip().lower() or "user"
    text = _content_to_text(message.get("content"))
    if not text.strip() and role == "assistant" and message.get("tool_calls"):
        text = ""  # 纯 tool_calls 的 assistant 消息没有正文
    return NormalizedMessage(role, text, message)


def normalize_messages(messages: Optional[Iterable[Any]]) -> list[NormalizedMessage]:
    return [normalize_message(m) for m in (messages or [])]


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def common_prefix_length(left: Sequence[NormalizedMessage],
                         right: Sequence[NormalizedMessage]) -> int:
    """最长公共前缀长度（按 role+文本 逐条比对）。"""
    limit = min(len(left), len(right))
    i = 0
    while i < limit and left[i].key == right[i].key:
        i += 1
    return i


def find_tail_anchor(stored: Sequence[NormalizedMessage],
                     incoming: Sequence[NormalizedMessage]) -> int:
    """在 incoming 里找 stored 最后一条消息的位置，返回其下标 +1；找不到返回 0。

    从后往前找，优先命中靠后的相同内容，避免历史里重复内容造成误判。
    """
    if not stored or not incoming:
        return 0
    target = stored[-1].key
    for idx in range(len(incoming) - 1, -1, -1):
        if incoming[idx].key == target:
            return idx + 1
    return 0


def _has_rewrite(stored: Sequence[NormalizedMessage],
                 incoming: Sequence[NormalizedMessage],
                 start: int, stop: int) -> bool:
    """重叠区间 ``[start, stop)`` 里是否出现了同位置但内容不同的消息（=客户端改写过）。"""
    for idx in range(start, min(stop, len(stored))):
        if incoming[idx].key != stored[idx].key:
            return True
    return False


def _trailing_payload(messages: Sequence[NormalizedMessage]) -> list[NormalizedMessage]:
    """取"最新一段可转发内容": 最后一条 user 起（含其后的 tool 结果）。"""
    start = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].role == "user":
            start = idx
            break
    if start < 0:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].role in FORWARD_ROLES:
                start = idx
                break
    if start < 0:
        return []
    return [m for m in messages[start:] if m.role in FORWARD_ROLES and m.text.strip()]


def join_payload(messages: Sequence[NormalizedMessage]) -> str:
    return "\n\n".join(m.text.strip() for m in messages if m.text.strip())


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def extract_new_content(stored: Optional[Iterable[Any]],
                        incoming: Optional[Iterable[Any]]) -> DedupeResult:
    """算出这次真正要转发的新内容。

    Args:
        stored: Redis 里存的上一轮完整历史（可为空 = 新会话）。
        incoming: 本次请求的 ``messages``。
    """
    incoming_n = normalize_messages(incoming)
    if not incoming_n:
        return DedupeResult(MODE_EMPTY, False, 0, [], "")

    stored_n = normalize_messages(stored)
    if not stored_n:
        payload = _trailing_payload(incoming_n)
        return DedupeResult(MODE_NEW_SESSION, True, 0, payload, join_payload(payload))

    prefix = common_prefix_length(stored_n, incoming_n)
    mode = MODE_PREFIX

    # 公共前缀之后、锚点之前若夹着"被改写过"的老消息，说明前缀不足以代表老历史，
    # 这时改用锚点（上一轮最后一条消息的位置）来切，才不会把老消息又发一遍。
    anchor = find_tail_anchor(stored_n, incoming_n)
    if anchor and anchor - 1 > prefix and _has_rewrite(stored_n, incoming_n, prefix, anchor - 1):
        prefix, mode = anchor, MODE_ANCHOR
    elif prefix == 0:
        if anchor:
            prefix, mode = anchor, MODE_ANCHOR
        else:
            # 客户端把历史整段重写了（改格式/删中间几条），只能保守取最新内容
            payload = _trailing_payload(incoming_n)
            return DedupeResult(MODE_REWRITTEN, False, 0, payload, join_payload(payload))

    tail = [m for m in incoming_n[prefix:] if m.role in FORWARD_ROLES and m.text.strip()]
    if not tail:
        return DedupeResult(MODE_NO_NEW_CONTENT, False, prefix, [], "")
    return DedupeResult(mode, False, prefix, tail, join_payload(tail))


def history_for_storage(incoming: Optional[Iterable[Any]]) -> list[dict[str, str]]:
    """存进 Redis 的历史快照（只留 role + 文本，别把原始大 payload 存进去）。"""
    return [m.to_dict() for m in normalize_messages(incoming)]


__all__ = [
    "FORWARD_ROLES",
    "MODE_NEW_SESSION",
    "MODE_PREFIX",
    "MODE_ANCHOR",
    "MODE_REWRITTEN",
    "MODE_NO_NEW_CONTENT",
    "MODE_EMPTY",
    "NormalizedMessage",
    "DedupeResult",
    "normalize_message",
    "normalize_messages",
    "common_prefix_length",
    "find_tail_anchor",
    "extract_new_content",
    "history_for_storage",
    "join_payload",
]
