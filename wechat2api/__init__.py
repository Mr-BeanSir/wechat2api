"""wechat2api —— 实机微信 <-> OpenAI 兼容接口。

对外只暴露一个 OpenAI 兼容的 HTTP 面（``/v1/chat/completions`` 等），
对内通过 wechatauto 驱动本机真实微信 4.x：读消息走本地 SQLCipher 解密，
发消息走 UIA（先对 ``Weixin.dll`` 打 Qt 无障碍热补丁），必要时退坐标/OCR。

会话 id 判定与去重规则对齐 sub2api，详见 ``session_id.py`` / ``dedupe.py``。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config
from .db_read import biz_shards, read_biz_rows
from .dedupe import DedupeResult, extract_new_content, history_for_storage
from .pipeline import Pipeline, PipelineError, TurnResult
from .session_id import ResolvedSession, resolve_session_id, short_id
from .store import BaseStore, MemoryStore, RedisStore, build_store
from .wechat_client import WeChatClient, WeChatHookError

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "Pipeline",
    "PipelineError",
    "TurnResult",
    "ResolvedSession",
    "resolve_session_id",
    "short_id",
    "DedupeResult",
    "extract_new_content",
    "history_for_storage",
    "BaseStore",
    "RedisStore",
    "MemoryStore",
    "build_store",
    "WeChatClient",
    "WeChatHookError",
    "read_biz_rows",
    "biz_shards",
]
