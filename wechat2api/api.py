"""对外的 OpenAI 兼容接口。

- ``POST /v1/chat/completions``  主入口（支持 stream=true 走 SSE）
- ``POST /v1/completions``       文本补全风格，转成一条 user 消息
- ``GET  /v1/models``
- ``GET  /health`` / ``GET /v1/admin/status`` / ``GET /v1/admin/sessions``
- ``POST /v1/admin/sessions/{sid}/reinit`` / ``DELETE /v1/admin/sessions/{sid}``
- ``POST /v1/admin/prompt/reload``

会话 id 按 sub2api 的优先级从请求头/请求体判定，判定结果会回写在响应头
``X-Session-Id`` / ``X-Session-Hash`` / ``X-Dedupe-Mode`` 里，方便调用方排查。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .config import Config, load_config
from .pipeline import Pipeline, PipelineError
from .session_id import short_id
from .store import build_store
from .wechat_client import WeChatClient

log = logging.getLogger("wechat2api.api")


def _header_value(value: str) -> str:
    """HTTP 头只能是 latin-1 且不能带控制字符；会话 id/转发内容可能含中文。

    非可打印 ASCII 一律百分号编码，保证响应能正常发出（也能原样还原）。
    """
    if not value:
        return ""
    if all(0x20 <= ord(ch) < 0x7F for ch in value):
        return value
    return quote(value, safe="")



def _rough_tokens(text: str) -> int:
    """粗略估 token（不引 tokenizer，仅用于 usage 字段可读）。"""
    if not text:
        return 0
    return max(1, int(len(text) / 2))


def _error(status: int, message: str, code: str = "wechat2api_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code}},
    )


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = build_store(cfg)
        wechat = WeChatClient(cfg)
        app.state.cfg = cfg
        app.state.store = store
        app.state.wechat = wechat
        app.state.pipeline = Pipeline(cfg, store, wechat)
        log.info(
            "wechat2api 就绪: 目标会话=%s 存储=%s 端口=%s",
            cfg.wechat.target,
            store.backend,
            cfg.server.port,
        )
        try:
            yield
        finally:
            wechat.close()
            store.close()

    app = FastAPI(title="wechat2api", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    def require_key(authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None)) -> None:
        expected = cfg.server.api_key
        if not expected:
            return
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        elif x_api_key:
            token = x_api_key.strip()
        if token != expected:
            raise HTTPException(status_code=401, detail="api key 不对")

    def get_pipeline(request: Request) -> Pipeline:
        pipe = getattr(request.app.state, "pipeline", None)
        if pipe is None:  # lifespan 没跑（极少见）
            raise HTTPException(status_code=503, detail="服务还没初始化完")
        return pipe

    def headers_of(request: Request) -> dict[str, str]:
        return {k.lower(): v for k, v in request.headers.items()}

    # ------------------------------------------------------------------
    @app.get("/health")
    async def health(request: Request) -> dict:
        store = getattr(request.app.state, "store", None)
        wechat = getattr(request.app.state, "wechat", None)
        return {
            "status": "ok",
            "store": getattr(store, "backend", "?"),
            "redis_ok": bool(store.ping()) if store else False,
            "wechat_connected": bool(getattr(wechat, "_chat", None)) if wechat else False,
            "target": cfg.wechat.target,
            "time": int(time.time()),
        }

    @app.get("/v1/models")
    async def models(_: None = Depends(require_key)) -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": cfg.server.model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "wechat2api",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        body: dict,
        _: None = Depends(require_key),
        pipe: Pipeline = Depends(get_pipeline),
    ):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _error(400, "messages 不能为空")

        stream = bool(body.get("stream"))
        model = str(body.get("model") or cfg.server.model_name)
        headers = headers_of(request)
        request_id = "req_" + uuid.uuid4().hex[:24]

        def work():
            return pipe.handle(headers=headers, body=body, request_id=request_id)

        try:
            turn = await run_in_threadpool(work)
        except PipelineError as exc:
            return _error(502, str(exc), "wechat_pipeline_error")
        except Exception as exc:  # pragma: no cover
            log.exception("处理失败")
            return _error(500, repr(exc), "internal_error")

        payload = _chat_payload(model, turn)
        extra = {
            "X-Session-Id": _header_value(turn.session_id),
            "X-Session-Hash": short_id(turn.session_id),
            "X-Dedupe-Mode": _header_value(str(turn.dedupe.get("mode", ""))),
            "X-Session-Source": _header_value(turn.source),
        }

        if not stream:
            return JSONResponse(content=payload, headers=extra)

        async def event_stream():
            chunk_id = payload["id"]
            created = payload["created"]
            delta_first = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": turn.reply}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(delta_first, ensure_ascii=False)}\n\n"
            final = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": payload["usage"],
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={**extra, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/completions")
    async def completions(
        request: Request,
        body: dict,
        _: None = Depends(require_key),
        pipe: Pipeline = Depends(get_pipeline),
    ):
        prompt = body.get("prompt")
        if isinstance(prompt, list):
            prompt = "\n".join(str(p) for p in prompt)
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(400, "prompt 不能为空")
        chat_body = dict(body)
        chat_body["messages"] = [{"role": "user", "content": prompt}]
        chat_body.pop("prompt", None)
        model = str(body.get("model") or cfg.server.model_name)
        headers = headers_of(request)
        request_id = "req_" + uuid.uuid4().hex[:24]

        try:
            turn = await run_in_threadpool(
                lambda: pipe.handle(headers=headers, body=chat_body, request_id=request_id)
            )
        except PipelineError as exc:
            return _error(502, str(exc), "wechat_pipeline_error")

        return JSONResponse(
            content={
                "id": "cmpl-" + uuid.uuid4().hex[:24],
                "object": "text_completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "text": turn.reply, "logprobs": None, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": _rough_tokens(prompt),
                    "completion_tokens": _rough_tokens(turn.reply),
                    "total_tokens": _rough_tokens(prompt) + _rough_tokens(turn.reply),
                },
            },
            headers={
                "X-Session-Id": _header_value(turn.session_id),
                "X-Session-Hash": short_id(turn.session_id),
                "X-Dedupe-Mode": _header_value(str(turn.dedupe.get("mode", ""))),
                "X-Session-Source": _header_value(turn.source),
            },
        )

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    @app.get("/v1/admin/status")
    async def admin_status(_: None = Depends(require_key),
                           pipe: Pipeline = Depends(get_pipeline)) -> dict:
        return pipe.status()

    @app.get("/v1/admin/sessions")
    async def admin_sessions(request: Request, _: None = Depends(require_key)) -> dict:
        store = request.app.state.store
        return {"active": store.get_active_session(), "sessions": store.list_sessions()}

    @app.post("/v1/admin/sessions/{session_id}/reinit")
    async def admin_reinit(session_id: str, request: Request,
                           _: None = Depends(require_key),
                           pipe: Pipeline = Depends(get_pipeline)) -> dict:
        try:
            ok, report = await run_in_threadpool(pipe.force_reinit, session_id)
        except Exception as exc:
            return _error(502, str(exc))
        return {"ok": ok, "init": report}

    @app.delete("/v1/admin/sessions/{session_id}")
    async def admin_delete(session_id: str, request: Request,
                           _: None = Depends(require_key)) -> dict:
        store = request.app.state.store
        store.delete_session(session_id)
        if store.get_active_session() == session_id:
            store.set_active_session("")
        return {"ok": True, "session_id": session_id}

    @app.post("/v1/admin/prompt/reload")
    async def admin_reload_prompt(_: None = Depends(require_key),
                                  pipe: Pipeline = Depends(get_pipeline)) -> dict:
        try:
            text = await run_in_threadpool(pipe.reload_prompt)
        except PipelineError as exc:
            return _error(500, str(exc))
        return {"ok": True, "chars": len(text)}

    return app


def _chat_payload(model: str, turn) -> dict:
    prompt_tokens = _rough_tokens(turn.forwarded or turn.reply)
    completion_tokens = _rough_tokens(turn.reply)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": turn.reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "wechat2api": {
            "session_id": turn.session_id,
            "session_hash": short_id(turn.session_id),
            "session_source": turn.source,
            "is_new_session": turn.is_new_session,
            "reinitialized": turn.reinitialized,
            "reused_reply": turn.reused_reply,
            "dedupe": turn.dedupe,
            "forwarded": turn.forwarded,
            "elapsed": round(turn.elapsed, 3),
        },
    }


__all__ = ["create_app"]
