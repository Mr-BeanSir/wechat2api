"""一次请求的完整处理流程。

``handle(headers, body)`` 做四步:

1. **判定会话 id** —— ``session_id.resolve_session_id``，与 sub2api 同款优先级。
2. **按需初始化** —— 新会话、或活跃会话 id 变了且开了 ``reinit_on_switch`` 时，
   先发 ``/clear`` 等回复，再发 ``prompt.txt`` 内容等回复；两步都完成才算
   ``initialized``。初始化用 Redis 锁互斥，避免并发请求各发一遍。
3. **去重** —— 拿 Redis 里存的上一轮历史跟本次 ``messages`` 对比，
   只把"最新内容"转发给微信。
4. **落库** —— 回复、完整历史、轮次写回 Redis。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .config import Config
from .dedupe import DedupeResult, extract_new_content, history_for_storage
from .session_id import SOURCE_FALLBACK, ResolvedSession, resolve_session_id, short_id
from .store import (
    STATE_HASH,
    STATE_LAST_FORWARD,
    STATE_LAST_SEEN,
    STATE_SOURCE,
    STATE_TURNS,
    BaseStore,
)
from .wechat_client import WeChatClient, WeChatHookError

log = logging.getLogger("wechat2api.pipeline")


class PipelineError(RuntimeError):
    """处理流程失败（会转成 HTTP 502）。"""


@dataclass
class TurnResult:
    session_id: str = ""
    session_hash: str = ""
    source: str = ""
    is_new_session: bool = False
    reinitialized: bool = False
    reused_reply: bool = False
    init: dict = field(default_factory=dict)
    dedupe: dict = field(default_factory=dict)
    forwarded: str = ""
    reply: str = ""
    history_len: int = 0
    turns: int = 0
    elapsed: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "session_hash": self.session_hash,
            "source": self.source,
            "is_new_session": self.is_new_session,
            "reinitialized": self.reinitialized,
            "reused_reply": self.reused_reply,
            "init": self.init,
            "dedupe": self.dedupe,
            "forwarded": self.forwarded,
            "reply": self.reply,
            "history_len": self.history_len,
            "turns": self.turns,
            "elapsed": round(self.elapsed, 3),
            "error": self.error,
        }


def extract_messages(body: Optional[Mapping[str, Any]]) -> list[dict]:
    """从请求体取消息列表，兼容 chat 的 ``messages`` 与 responses 的 ``input``。"""
    if not body:
        return []
    messages = body.get("messages")
    if isinstance(messages, list):
        return [dict(m) for m in messages if isinstance(m, Mapping)]
    inp = body.get("input")
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    if isinstance(inp, list):
        out: list[dict] = []
        for item in inp:
            if not isinstance(item, Mapping):
                if isinstance(item, str):
                    out.append({"role": "user", "content": item})
                continue
            if str(item.get("type", "")) == "input_text":
                out.append({"role": "user", "content": item.get("text") or ""})
                continue
            out.append(
                {
                    "role": str(item.get("role", "") or "user"),
                    "content": item.get("content") if item.get("content") is not None else item.get("text"),
                }
            )
        return out
    return []


class Pipeline:
    def __init__(self, cfg: Config, store: BaseStore, wechat: WeChatClient):
        self.cfg = cfg
        self.store = store
        self.wechat = wechat
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._prompt_cache: Optional[str] = None

    # ------------------------------------------------------------------
    def prompt_text(self) -> str:
        if self._prompt_cache is None:
            try:
                self._prompt_cache = self.cfg.prompt_text()
            except FileNotFoundError as exc:
                raise PipelineError(f"读不到初始化提示词文件: {self.cfg.prompt_path}") from exc
            if not self._prompt_cache:
                log.warning("prompt.txt 是空的，初始化只会发 %s", self.cfg.wechat.init_command)
        return self._prompt_cache

    def reload_prompt(self) -> str:
        self._prompt_cache = None
        return self.prompt_text()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    # ------------------------------------------------------------------
    def handle(self, headers: Optional[Mapping[str, Any]] = None,
               body: Optional[Mapping[str, Any]] = None,
               request_id: str = "") -> TurnResult:
        started = time.time()
        resolved: ResolvedSession = resolve_session_id(headers, body, request_id=request_id)
        result = TurnResult(
            session_id=resolved.session_id,
            session_hash=resolved.hash,
            source=resolved.source,
        )
        messages = extract_messages(body)
        log.info(
            "请求: 会话=%s(来源=%s) 消息=%d 条",
            short_id(resolved.session_id),
            resolved.source,
            len(messages),
        )

        with self._session_lock(resolved.session_id):
            try:
                result.reinitialized, result.init = self._ensure_session(resolved)
            except WeChatHookError as exc:
                result.error = str(exc)
                result.elapsed = time.time() - started
                raise PipelineError(f"会话初始化失败: {exc}") from exc

            outcome = self._run_turn(resolved, messages)
            result.is_new_session = outcome["is_new_session"]
            result.dedupe = outcome["dedupe"]
            result.forwarded = outcome["forwarded"]
            result.reply = outcome["reply"]
            result.reused_reply = outcome["reused_reply"]
            result.history_len = outcome["history_len"]
            result.turns = outcome["turns"]

        result.elapsed = time.time() - started
        log.info(
            "完成: 会话=%s 模式=%s 转发=%d 字 回复=%d 字 用时=%.2fs",
            short_id(resolved.session_id),
            result.dedupe.get("mode", "-"),
            len(result.forwarded),
            len(result.reply),
            result.elapsed,
        )
        return result

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def force_reinit(self, session_id: str, source: str = SOURCE_FALLBACK) -> tuple[bool, dict]:
        """管理接口用：不看 initialized 标记，强制重跑一遍 /clear + prompt.txt。"""
        resolved = ResolvedSession(session_id, source)
        with self._session_lock(session_id):
            return self._ensure_session(resolved, force=True)

    def _ensure_session(self, resolved: ResolvedSession, force: bool = False) -> tuple[bool, dict]:
        """返回 (是否刚做过初始化, 初始化报告)。"""
        cfg = self.cfg.wechat
        sid = resolved.session_id
        state = self.store.get_state(sid)
        initialized = bool(state.get("initialized"))
        active = self.store.get_active_session()
        switched = bool(active) and active != sid

        if initialized and not force and not (switched and cfg.reinit_on_switch):
            if active != sid:  # 会话没变，仅同步活跃标记
                self.store.set_active_session(sid)
            return False, {}

        reason = "强制重初始化" if force else ("新会话" if not initialized else "活跃会话切换")
        log.info("%s（%s），开始初始化: %s -> prompt.txt", reason, short_id(sid), cfg.init_command)

        lock_ttl = max(60.0, cfg.init_command_timeout + cfg.init_prompt_timeout + 30.0)
        if not self.store.try_acquire_init_lock(sid, ttl=lock_ttl):
            log.info("别的请求正在初始化 %s，等它做完", short_id(sid))
            if self._wait_initialized(sid, timeout=cfg.init_prompt_timeout):
                return False, {"waited": True}
            log.warning("等待初始化超时，自行初始化 %s", short_id(sid))
            if not self.store.try_acquire_init_lock(sid, ttl=lock_ttl):
                return False, {"waited": True, "skipped": True}

        try:
            report = self.wechat.initialize_session(sid, self.prompt_text(), command=cfg.init_command)
        finally:
            self.store.release_init_lock(sid)

        payload = report.to_dict()
        if report.ok:
            self.store.mark_initialized(sid, source=resolved.source)
            self.store.update_state(sid, **{STATE_HASH: resolved.hash})
            self.store.set_active_session(sid)
            log.info("初始化完成: %s", short_id(sid))
            return True, payload

        if cfg.init_require_reply:
            raise WeChatHookError(report.error or "初始化未完成")
        log.warning("初始化未完美完成，继续: %s", report.error)
        self.store.mark_initialized(sid, source=resolved.source)
        self.store.set_active_session(sid)
        return True, payload

    def _wait_initialized(self, session_id: str, timeout: float) -> bool:
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if self.store.is_initialized(session_id):
                return True
            time.sleep(0.5)
        return self.store.is_initialized(session_id)

    # ------------------------------------------------------------------
    # 转发 + 回复
    # ------------------------------------------------------------------
    def _run_turn(self, resolved: ResolvedSession, messages: list[dict]) -> dict:
        sid = resolved.session_id
        stored = self.store.get_history(sid)
        dedupe: DedupeResult = extract_new_content(stored, messages)
        log.debug("去重结果: %s", dedupe.to_dict())

        reply = ""
        reused = False
        if dedupe.has_new_content:
            try:
                reply = self.wechat.send_and_wait(dedupe.new_text)
            except WeChatHookError as exc:
                self.store.set_history(sid, history_for_storage(messages))
                state = self.store.get_state(sid)
                self.store.update_state(
                    sid,
                    **{
                        STATE_TURNS: int(state.get(STATE_TURNS) or 0) + 1,
                        STATE_LAST_SEEN: int(time.time()),
                        STATE_HASH: resolved.hash,
                        STATE_SOURCE: resolved.source,
                        STATE_LAST_FORWARD: dedupe.new_text[:200],
                    },
                )
                raise PipelineError(f"转发失败: {exc}") from exc
            self.store.set_last_reply(sid, reply)
        else:
            reply = self.store.get_last_reply(sid)
            reused = True
            if not reply:
                log.info("本次没有新内容，且没有可复用的回复")

        self.store.set_history(sid, history_for_storage(messages))
        state = self.store.get_state(sid)
        turns = int(state.get(STATE_TURNS) or 0) + 1
        self.store.update_state(
            sid,
            **{
                STATE_TURNS: turns,
                STATE_LAST_SEEN: int(time.time()),
                STATE_HASH: resolved.hash,
                STATE_SOURCE: resolved.source,
                STATE_LAST_FORWARD: dedupe.new_text[:200],
            },
        )
        return {
            "is_new_session": dedupe.is_new_session,
            "dedupe": dedupe.to_dict(),
            "forwarded": dedupe.new_text,
            "reply": reply,
            "reused_reply": reused,
            "history_len": len(messages),
            "turns": turns,
        }

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "active_session": self.store.get_active_session(),
            "active_hash": short_id(self.store.get_active_session()) if self.store.get_active_session() else "",
            "store": self.store.backend,
            "target": self.cfg.wechat.target,
            "init_command": self.cfg.wechat.init_command,
            "reinit_on_switch": self.cfg.wechat.reinit_on_switch,
            "prompt_chars": len(self._prompt_cache or "") if self._prompt_cache else 0,
            "sessions": len(self.store.list_sessions()),
            "wechat": self.wechat.status(),
        }


__all__ = ["Pipeline", "PipelineError", "TurnResult", "extract_messages"]
