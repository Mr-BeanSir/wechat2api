"""会话状态存储（Redis 为主，可选进程内存兜底）。

每个会话 id 一份状态，键结构::

    {prefix}:hist:{sid}      上一轮完整聊天历史（JSON 数组）
    {prefix}:state:{sid}     会话元信息 hash: initialized / turns / created_at ...
    {prefix}:reply:{sid}     上一轮微信回复（没有新内容时直接复用）
    {prefix}:initlock:{sid}  初始化互斥锁（SET NX EX）
    {prefix}:active          当前"活跃会话 id"（微信只有一个账号，靠它判切换）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from .config import Config

log = logging.getLogger("wechat2api.store")

STATE_INITIALIZED = "initialized"
STATE_TURNS = "turns"
STATE_CREATED_AT = "created_at"
STATE_LAST_SEEN = "last_seen"
STATE_INIT_AT = "init_at"
STATE_SOURCE = "source"
STATE_HASH = "hash"
STATE_LAST_FORWARD = "last_forward"


class BaseStore:
    """存储接口。"""

    backend = "base"

    # -- 历史 ---------------------------------------------------------------
    def get_history(self, session_id: str) -> list[dict]:
        raise NotImplementedError

    def set_history(self, session_id: str, messages: list[dict]) -> None:
        raise NotImplementedError

    # -- 状态 ---------------------------------------------------------------
    def get_state(self, session_id: str) -> dict:
        raise NotImplementedError

    def update_state(self, session_id: str, **fields: Any) -> None:
        state = self.get_state(session_id)
        state.update(fields)
        self._write_state(session_id, state)

    def _write_state(self, session_id: str, state: dict) -> None:
        raise NotImplementedError

    def delete_session(self, session_id: str) -> None:
        raise NotImplementedError

    def list_sessions(self) -> list[dict]:
        raise NotImplementedError

    # -- 回复 ---------------------------------------------------------------
    def get_last_reply(self, session_id: str) -> str:
        raise NotImplementedError

    def set_last_reply(self, session_id: str, text: str) -> None:
        raise NotImplementedError

    # -- 活跃会话 -----------------------------------------------------------
    def get_active_session(self) -> str:
        raise NotImplementedError

    def set_active_session(self, session_id: str) -> None:
        raise NotImplementedError

    def clear_active_session(self, session_id: str = "") -> bool:
        """清掉活跃标记。给了 session_id 时只在"当前活跃值就是它"时才清，返回是否清掉了。"""
        raise NotImplementedError

    # -- 初始化锁 -----------------------------------------------------------
    def try_acquire_init_lock(self, session_id: str, ttl: float = 180.0) -> bool:
        raise NotImplementedError

    def release_init_lock(self, session_id: str) -> None:
        raise NotImplementedError

    # -- 杂项 ---------------------------------------------------------------
    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass

    # 便于调用方统一处理"是否已初始化"
    def is_initialized(self, session_id: str) -> bool:
        return bool(self.get_state(session_id).get(STATE_INITIALIZED))

    def mark_initialized(self, session_id: str, source: str = "") -> None:
        self.update_state(
            session_id,
            **{
                STATE_INITIALIZED: True,
                STATE_INIT_AT: int(time.time()),
                STATE_SOURCE: source,
            },
        )


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

class RedisStore(BaseStore):
    backend = "redis"

    def __init__(self, url: str, prefix: str = "wechat2api", ttl_seconds: int = 0):
        import redis  # 延迟导入

        self._ttl = int(ttl_seconds or 0)
        self._prefix = prefix.rstrip(":")
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
        self._client.ping()

    # -- key helpers --------------------------------------------------------
    def _k(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

    def _hist_key(self, sid: str) -> str:
        return self._k("hist", sid)

    def _state_key(self, sid: str) -> str:
        return self._k("state", sid)

    def _reply_key(self, sid: str) -> str:
        return self._k("reply", sid)

    def _lock_key(self, sid: str) -> str:
        return self._k("initlock", sid)

    def _active_key(self) -> str:
        return self._k("active")

    def _touch(self, *keys: str) -> None:
        if self._ttl > 0:
            for key in keys:
                try:
                    self._client.expire(key, self._ttl)
                except Exception:  # pragma: no cover - 过期失败不该影响主流程
                    pass

    # -- 历史 ---------------------------------------------------------------
    def get_history(self, session_id: str) -> list[dict]:
        raw = self._client.get(self._hist_key(session_id))
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (TypeError, ValueError):
            log.warning("会话 %s 的历史 JSON 解析失败，按空历史处理", session_id)
            return []

    def set_history(self, session_id: str, messages: list[dict]) -> None:
        key = self._hist_key(session_id)
        self._client.set(key, json.dumps(messages, ensure_ascii=False))
        self._touch(key)

    # -- 状态 ---------------------------------------------------------------
    def get_state(self, session_id: str) -> dict:
        raw = self._client.hgetall(self._state_key(session_id)) or {}
        return _decode_state(raw)

    def _write_state(self, session_id: str, state: dict) -> None:
        key = self._state_key(session_id)
        payload = {k: _encode_value(v) for k, v in state.items() if v is not None}
        if payload:
            self._client.hset(key, mapping=payload)
        self._touch(key)

    def delete_session(self, session_id: str) -> None:
        self._client.delete(
            self._hist_key(session_id),
            self._state_key(session_id),
            self._reply_key(session_id),
            self._lock_key(session_id),
        )

    def list_sessions(self) -> list[dict]:
        pattern = self._k("state", "*")
        out: list[dict] = []
        prefix = self._k("state", "")
        for key in self._client.scan_iter(match=pattern, count=200):
            sid = key[len(prefix):] if isinstance(key, str) else str(key)[len(prefix):]
            state = self.get_state(sid)
            out.append(
                {
                    "session_id": sid,
                    "hash": state.get(STATE_HASH) or "",
                    "initialized": bool(state.get(STATE_INITIALIZED)),
                    "turns": int(state.get(STATE_TURNS) or 0),
                    "last_seen": int(state.get(STATE_LAST_SEEN) or 0),
                    "history_len": len(self.get_history(sid)),
                    "last_reply": (self.get_last_reply(sid) or "")[:200],
                }
            )
        out.sort(key=lambda item: item["last_seen"], reverse=True)
        return out

    # -- 回复 ---------------------------------------------------------------
    def get_last_reply(self, session_id: str) -> str:
        return self._client.get(self._reply_key(session_id)) or ""

    def set_last_reply(self, session_id: str, text: str) -> None:
        key = self._reply_key(session_id)
        self._client.set(key, text or "")
        self._touch(key)

    # -- 活跃会话 -----------------------------------------------------------
    def get_active_session(self) -> str:
        return self._client.get(self._active_key()) or ""

    def set_active_session(self, session_id: str) -> None:
        self._client.set(self._active_key(), session_id or "")

    def clear_active_session(self, session_id: str = "") -> bool:
        key = self._active_key()
        if session_id and (self._client.get(key) or "") != session_id:
            return False
        self._client.delete(key)
        return True

    # -- 初始化锁 -----------------------------------------------------------
    def try_acquire_init_lock(self, session_id: str, ttl: float = 180.0) -> bool:
        return bool(
            self._client.set(self._lock_key(session_id), str(time.time()), nx=True, ex=max(1, int(ttl)))
        )

    def release_init_lock(self, session_id: str) -> None:
        self._client.delete(self._lock_key(session_id))

    # -- 杂项 ---------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 进程内存兜底
# ---------------------------------------------------------------------------

class MemoryStore(BaseStore):
    backend = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hist: dict[str, list[dict]] = {}
        self._state: dict[str, dict] = {}
        self._reply: dict[str, str] = {}
        self._locks: dict[str, float] = {}
        self._active = ""

    def get_history(self, session_id: str) -> list[dict]:
        with self._lock:
            return list(self._hist.get(session_id) or [])

    def set_history(self, session_id: str, messages: list[dict]) -> None:
        with self._lock:
            self._hist[session_id] = list(messages or [])

    def get_state(self, session_id: str) -> dict:
        with self._lock:
            return dict(self._state.get(session_id) or {})

    def _write_state(self, session_id: str, state: dict) -> None:
        with self._lock:
            self._state[session_id] = {k: v for k, v in state.items() if v is not None}

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._hist.pop(session_id, None)
            self._state.pop(session_id, None)
            self._reply.pop(session_id, None)
            self._locks.pop(session_id, None)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            out = []
            for sid, state in self._state.items():
                out.append(
                    {
                        "session_id": sid,
                        "hash": state.get(STATE_HASH) or "",
                        "initialized": bool(state.get(STATE_INITIALIZED)),
                        "turns": int(state.get(STATE_TURNS) or 0),
                        "last_seen": int(state.get(STATE_LAST_SEEN) or 0),
                        "history_len": len(self._hist.get(sid) or []),
                        "last_reply": (self._reply.get(sid) or "")[:200],
                    }
                )
            out.sort(key=lambda item: item["last_seen"], reverse=True)
            return out

    def get_last_reply(self, session_id: str) -> str:
        with self._lock:
            return self._reply.get(session_id, "")

    def set_last_reply(self, session_id: str, text: str) -> None:
        with self._lock:
            self._reply[session_id] = text or ""

    def get_active_session(self) -> str:
        with self._lock:
            return self._active

    def set_active_session(self, session_id: str) -> None:
        with self._lock:
            self._active = session_id or ""

    def clear_active_session(self, session_id: str = "") -> bool:
        with self._lock:
            if session_id and self._active != session_id:
                return False
            self._active = ""
            return True

    def try_acquire_init_lock(self, session_id: str, ttl: float = 180.0) -> bool:
        with self._lock:
            now = time.time()
            held = self._locks.get(session_id, 0.0)
            if held and now - held < ttl:
                return False
            self._locks[session_id] = now
            return True

    def release_init_lock(self, session_id: str) -> None:
        with self._lock:
            self._locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def _encode_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _decode_state(raw: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key == STATE_INITIALIZED:
            out[key] = str(value).lower() in ("1", "true", "yes")
        elif key in (STATE_TURNS, STATE_CREATED_AT, STATE_LAST_SEEN, STATE_INIT_AT):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                out[key] = 0
        else:
            out[key] = value
    return out


def build_store(cfg: Config) -> BaseStore:
    """按配置建存储；Redis 不可用时根据 ``redis.require`` 决定报错还是兜底。"""
    try:
        store = RedisStore(cfg.redis.url, cfg.redis.prefix, cfg.redis.ttl_seconds)
        log.info("使用 Redis 存储: %s (prefix=%s)", cfg.redis.url, cfg.redis.prefix)
        return store
    except Exception as exc:
        message = f"Redis 不可用({cfg.redis.url}): {exc!r}"
        if cfg.redis.require:
            raise RuntimeError(message) from exc
        log.warning("%s —— 退回进程内存存储（重启即丢，仅适合调试）", message)
        return MemoryStore()


__all__ = [
    "BaseStore",
    "RedisStore",
    "MemoryStore",
    "build_store",
    "STATE_INITIALIZED",
    "STATE_TURNS",
    "STATE_LAST_SEEN",
    "STATE_INIT_AT",
    "STATE_SOURCE",
    "STATE_HASH",
    "STATE_LAST_FORWARD",
]
