"""端到端自检（不碰真微信）：用假的微信传输层跑完整 pipeline + HTTP 接口。

    .venv\\Scripts\\python.exe tools\\e2e_fake.py

覆盖：
  * 新会话 -> 自动跑 /clear + prompt.txt 初始化
  * 只转发"最新内容"（codex 会重复发全量历史）
  * 同一份历史重发时复用上一次回复
  * 会话 id 变了 -> 重新初始化
  * /v1/chat/completions（含 stream）、/v1/completions、/v1/models、管理接口、鉴权
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from wechat2api import api as api_mod
from wechat2api.config import load_config
from wechat2api.store import MemoryStore
from wechat2api.wechat_client import InitReport

PASS = 0
FAIL = 0
KEY = "sk-selftest"
AUTH = {"Authorization": f"Bearer {KEY}"}
PROMPT = "你是微信里的助手，简短回答。"


def check(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra!r}")


class FakeWeChat:
    """替掉真机 hook：只记录发了什么，按脚本回话。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.init_calls: list[dict] = []
        self.sent: list[str] = []
        self.closed = False

    def initialize_session(self, session_id, prompt_text, command=None, **kwargs):
        self.init_calls.append({"session": session_id, "command": command, "prompt": prompt_text})
        return InitReport(
            session_id=session_id,
            command=command or "",
            command_reply="已清空上下文",
            prompt_chars=len(prompt_text or ""),
            prompt_reply="好的，我记住了",
            ok=True,
            steps=[{"step": "command", "ok": True}, {"step": "prompt", "ok": True}],
        )

    def send_and_wait(self, text, timeout=None, settle=None, require_reply=True, **kwargs):
        self.sent.append(text)
        return f"收到：{text[:10]}"

    def status(self):
        return {"fake": True, "sent": len(self.sent), "inits": len(self.init_calls)}

    def close(self):
        self.closed = True


def build_client() -> tuple[TestClient, FakeWeChat, api_mod.Config]:
    cfg = load_config()
    cfg.server.api_key = KEY
    cfg.wechat.init_require_reply = True
    holder: dict[str, FakeWeChat] = {}

    def fake_wechat_factory(_cfg):
        holder["fake"] = FakeWeChat(_cfg)
        return holder["fake"]

    api_mod.build_store = lambda _cfg: MemoryStore()
    api_mod.WeChatClient = fake_wechat_factory
    app = api_mod.create_app(cfg)
    return TestClient(app), holder, cfg


def main() -> int:
    client, holder, cfg = build_client()

    with client:
        fake = holder["fake"]
        prompt_file = cfg.prompt_path

        # ---------------- 基础接口 ----------------
        print("[接口]")
        r = client.get("/health")
        check("GET /health 200", r.status_code == 200, r.text)
        check("health 报告存储后端", r.json().get("store") == "memory", r.text)

        r = client.get("/v1/models", headers=AUTH)
        check("GET /v1/models 200", r.status_code == 200, r.text)
        check("模型名来自配置", r.json()["data"][0]["id"] == cfg.server.model_name, r.text)

        r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        check("错误 api key 401", r.status_code == 401, r.text)
        r = client.get("/v1/models", headers={"X-API-Key": KEY})
        check("X-API-Key 也认", r.status_code == 200, r.text)

        # ---------------- 第一轮：新会话 ----------------
        print("[第一轮：新会话]")
        t1 = {
            "model": "gpt-5-codex",
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": "帮我写个快排"},
            ],
        }
        r = client.post("/v1/chat/completions", json=t1, headers=AUTH)
        check("chat/completions 200", r.status_code == 200, r.text)
        body = r.json()
        diag = body.get("wechat2api", {})
        check("object 是 chat.completion", body.get("object") == "chat.completion", body.get("object"))
        check("回复透传", body["choices"][0]["message"]["content"] == "收到：帮我写个快排", body)
        check("判定为新会话", diag.get("is_new_session") is True, diag)
        check("做了初始化", diag.get("reinitialized") is True, diag)
        check("初始化先发 /clear 再发 prompt.txt",
              len(fake.init_calls) == 1
              and fake.init_calls[0]["command"] == "/clear"
              and set(fake.init_calls[0]["prompt"].splitlines()) == set(
                  open(prompt_file, encoding="utf-8").read().strip().splitlines() or [""]),
              fake.init_calls)
        check("只转发最新内容", diag.get("forwarded") == "帮我写个快排", diag)
        check("会话来自内容种子", diag.get("session_source") == "content-seed", diag)
        check("响应头带会话信息",
              r.headers.get("x-session-source") == "content-seed"
              and r.headers.get("x-dedupe-mode") == "new_session"
              and len(r.headers.get("x-session-hash", "")) == 12,
              dict(r.headers))
        sid1 = diag.get("session_id")
        check("微信端收到 1 条", fake.sent == ["帮我写个快排"], fake.sent)
        check("响应头里的会话 id 可还原（百分号编码）",
              unquote(r.headers.get("x-session-id", "")) == sid1, 
              (r.headers.get("x-session-id"), sid1))

        # ---------------- 第二轮：追加一轮 ----------------
        print("[第二轮：客户端带全量历史]")
        t2 = {
            "model": "gpt-5-codex",
            "messages": t1["messages"] + [
                {"role": "assistant", "content": "收到：帮我写个快排"},
                {"role": "user", "content": "改成从大到小"},
            ],
        }
        r = client.post("/v1/chat/completions", json=t2, headers=AUTH)
        diag2 = r.json()["wechat2api"]
        check("会话没变", diag2.get("session_id") == sid1, diag2.get("session_id"))
        check("不重复初始化", diag2.get("reinitialized") is False, diag2)
        check("只转发新的一条", diag2.get("forwarded") == "改成从大到小", diag2)
        check("去重模式 prefix", r.headers.get("x-dedupe-mode") == "prefix", dict(r.headers))
        check("微信端累计 2 条", fake.sent == ["帮我写个快排", "改成从大到小"], fake.sent)
        check("初始化只跑过一次", len(fake.init_calls) == 1, fake.init_calls)

        # ---------------- 第三轮：原样重发 ----------------
        print("[第三轮：原样重发（应复用上次回复）]")
        r = client.post("/v1/chat/completions", json=t2, headers=AUTH)
        diag3 = r.json()["wechat2api"]
        check("没有新内容", r.headers.get("x-dedupe-mode") == "no_new_content", dict(r.headers))
        check("复用上次回复", diag3.get("reused_reply") is True
              and r.json()["choices"][0]["message"]["content"] == "收到：改成从大到小", diag3)
        check("没有重复发给微信", len(fake.sent) == 2, fake.sent)

        # ---------------- 第四轮：换会话 ----------------
        print("[第四轮：首条 user 变了 -> 新会话]")
        t4 = {"model": "gpt-5-codex", "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": "另一个全新的问题"},
        ]}
        r = client.post("/v1/chat/completions", json=t4, headers=AUTH)
        diag4 = r.json()["wechat2api"]
        check("会话 id 变了", diag4.get("session_id") != sid1, diag4.get("session_id"))
        check("触发重新初始化", diag4.get("reinitialized") is True, diag4)
        check("初始化跑了两次", len(fake.init_calls) == 2, fake.init_calls)
        check("转发新会话内容", diag4.get("forwarded") == "另一个全新的问题", diag4)

        # ---------------- 显式会话头 ----------------
        print("[显式会话头]")
        t5 = {"model": "gpt-5-codex", "messages": [{"role": "user", "content": "带头部的请求"}]}
        r = client.post("/v1/chat/completions", json=t5, headers={**AUTH, "session-id": "codex-abc"})
        diag5 = r.json()["wechat2api"]
        check("用 session-id 头", diag5.get("session_id") == "codex-abc"
              and diag5.get("session_source") == "header", diag5)
        check("新 id 再初始化", diag5.get("reinitialized") is True, diag5)
        r = client.post("/v1/chat/completions", json=t5, headers={**AUTH, "session-id": "codex-abc"})
        check("同一个头二次请求不再初始化",
              r.json()["wechat2api"].get("reinitialized") is False, r.json()["wechat2api"])

        # ---------------- 流式 ----------------
        print("[流式]")
        t6 = {"model": "gpt-5-codex", "stream": True,
              "messages": [{"role": "user", "content": "流式问题"}]}
        with client.stream("POST", "/v1/chat/completions", json=t6, headers=AUTH) as resp:
            check("stream 200", resp.status_code == 200, resp.status_code)
            check("content-type 是 SSE", resp.headers.get("content-type", "").startswith("text/event-stream"),
                  resp.headers.get("content-type"))
            text = "".join(resp.iter_text())
        check("SSE 以 [DONE] 收尾", text.strip().endswith("data: [DONE]"), text[-200:])
        check("SSE 里有正文增量", "流式问题" in text and '"delta"' in text, text[:200])

        # ---------------- /v1/completions ----------------
        print("[/v1/completions]")
        r = client.post("/v1/completions", json={"model": "gpt-3.5-turbo-instruct", "prompt": "补全一下"},
                        headers=AUTH)
        check("completions 200", r.status_code == 200, r.text)
        check("object 是 text_completion", r.json().get("object") == "text_completion", r.text)
        check("正文在 choices[0].text", r.json()["choices"][0]["text"].startswith("收到：补全一下"), r.text)
        check("空 prompt 400",
              client.post("/v1/completions", json={"prompt": "  "}, headers=AUTH).status_code == 400)

        # ---------------- 空 messages ----------------
        check("空 messages 400",
              client.post("/v1/chat/completions", json={"messages": []}, headers=AUTH).status_code == 400)

        # ---------------- 管理接口 ----------------
        print("[管理接口]")
        r = client.get("/v1/admin/status", headers=AUTH)
        check("admin/status 200", r.status_code == 200, r.text)
        st = r.json()
        check("状态含目标会话", st.get("target") == cfg.wechat.target, st)
        check("状态含微信侧信息", st.get("wechat", {}).get("fake") is True, st)

        r = client.get("/v1/admin/sessions", headers=AUTH)
        check("admin/sessions 至少 5 个会话", len(r.json().get("sessions", [])) >= 5, r.text)
        check("会话条目带 initialized", all("initialized" in s for s in r.json()["sessions"]), r.text)

        r = client.post("/v1/admin/sessions/codex-abc/reinit", headers=AUTH)
        check("reinit 200", r.status_code == 200, r.text)
        check("reinit 真的重跑", r.json().get("ok") is True and len(fake.init_calls) == 6, fake.init_calls)

        r = client.post("/v1/admin/prompt/reload", headers=AUTH)
        check("prompt/reload 200", r.status_code == 200, r.text)
        check("reload 读到了 prompt.txt", r.json().get("chars", 0) > 0, r.text)

        r = client.delete("/v1/admin/sessions/codex-abc", headers=AUTH)
        check("delete session 200", r.status_code == 200, r.text)

        r = client.post("/v1/admin/prompt/reload", headers={"Authorization": "Bearer nope"})
        check("管理接口也要鉴权", r.status_code == 401, r.status_code)

        # ---------------- 提示词文件缺失 ----------------
        print("[异常路径]")
        from wechat2api.pipeline import Pipeline, PipelineError
        pipe = Pipeline(cfg, MemoryStore(), fake)
        pipe._prompt_cache = None
        cfg.wechat.prompt_file = "no-such-file.txt"
        try:
            pipe.prompt_text()
            check("prompt.txt 缺失时报 PipelineError", False, "没报错")
        except PipelineError as exc:
            check("prompt.txt 缺失时报 PipelineError", "no-such-file" in str(exc), str(exc))
        finally:
            cfg.wechat.prompt_file = "prompt.txt"

    check("退出时关掉了微信连接", holder["fake"].closed)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
