"""离线自检：不碰微信，只验 session_id / dedupe / store 三个纯逻辑模块 + 节流档位配置。

    .venv\\Scripts\\python.exe tools\\selftest.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat2api.config import load_config
from wechat2api.dedupe import extract_new_content, history_for_storage
from wechat2api.session_id import (
    MAX_SESSION_ID_LENGTH,
    compact_content_seed,
    derive_content_session_seed,
    resolve_session_id,
    sanitize_session_id,
    short_id,
)
from wechat2api.store import build_store

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def test_session_id() -> None:
    print("[session_id]")
    r = resolve_session_id({"session-id": "abc-123"}, {})
    check("session-id 头", r.session_id == "abc-123" and r.source == "header", r)

    r = resolve_session_id({"x-session-affinity": "aff-1"}, {})
    check("X-Session-Affinity 头", r.session_id == "aff-1" and r.source == "header", r)

    r = resolve_session_id({"x-claude-code-session-id": "claude-9"}, {})
    check("Claude Code 头", r.session_id == "claude-9" and r.source == "claude-header", r)

    r = resolve_session_id({"x-grok-conv-id": "grok-7"}, {"model": "gpt-4o"})
    check("Grok 头对非 grok 模型不生效", r.session_id != "grok-7", r)

    r = resolve_session_id({"x-grok-conv-id": "grok-7"}, {"model": "grok-3"})
    check("Grok 头对 grok 模型生效", r.session_id == "grok-7" and r.source == "grok-header", r)

    r = resolve_session_id({}, {"prompt_cache_key": "pck-1"})
    check("prompt_cache_key", r.session_id == "pck-1" and r.source == "prompt_cache_key", r)

    body = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "第一句"},
        ],
    }
    r1 = resolve_session_id({}, body)
    seed = derive_content_session_seed(body)
    check("内容种子前缀", seed.startswith("compat_cs_"), seed)
    check("内容种子来源", r1.source == "content-seed", r1)

    body2 = dict(body)
    body2["messages"] = body["messages"] + [
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "第二句"},
    ]
    r2 = resolve_session_id({}, body2)
    check("同一会话多轮 id 不变", r1.session_id == r2.session_id, f"{r1.session_id} != {r2.session_id}")
    check("同一会话多轮 hash 不变", r1.hash == r2.hash)

    body3 = dict(body)
    body3["messages"] = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "换一句"}]
    r3 = resolve_session_id({}, body3)
    check("首轮 user 变了 id 就变", r1.session_id != r3.session_id)

    check("hash 是 16 位十六进制", len(r1.hash) == 16 and all(c in "0123456789abcdef" for c in r1.hash), r1.hash)
    check("legacy hash 是 64 位 sha256", len(r1.legacy_hash) == 64, len(r1.legacy_hash))
    check("short_id 取前 12 位", short_id(r1.session_id) == r1.hash[:12])

    check("去空白", sanitize_session_id("  x  ") == "x")
    check("空值被拒", sanitize_session_id("") is None)
    check("控制字符被拒", sanitize_session_id("a\x01b") is None)
    check("超长被拒", sanitize_session_id("z" * 256) is None)
    check("255 刚好通过", sanitize_session_id("z" * 255) is not None)

    r = resolve_session_id({}, {}, request_id="req_abc")
    check("兜底用 request_id", r.session_id == "req_abc" and r.source == "fallback", r)
    r = resolve_session_id({}, {})
    check("什么都没有也兜底", bool(r.session_id) and r.source == "fallback", r)

    # 长内容种子：压成 compat_cs_<哈希>，稳定、可放 HTTP 头
    long_body = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "很长的提示词" * 60},
            {"role": "user", "content": "第一句"},
        ],
    }
    raw_seed = derive_content_session_seed(long_body)
    check("长种子本身确实超长", len(raw_seed) > MAX_SESSION_ID_LENGTH, len(raw_seed))
    lr1 = resolve_session_id({}, long_body)
    check("长种子被压缩", len(lr1.session_id) == len("compat_cs_") + 16
          and lr1.session_id.startswith("compat_cs_"), lr1.session_id)
    same = dict(long_body)
    same["messages"] = long_body["messages"] + [{"role": "assistant", "content": "嗯"},
                                                {"role": "user", "content": "第二句"}]
    check("压缩后仍然稳定", resolve_session_id({}, same).session_id == lr1.session_id)
    other = dict(long_body)
    other["messages"] = [long_body["messages"][0], {"role": "user", "content": "完全不同的问题"}]
    check("压缩后不同会话仍区分", resolve_session_id({}, other).session_id != lr1.session_id)
    check("压缩后没有非 latin-1 字符",
          all(0x20 <= ord(c) < 0x7F for c in lr1.session_id), lr1.session_id)
    check("短种子不压缩", compact_content_seed("compat_cs_short") == "compat_cs_short")
    check("压缩结果与短种子不会撞（前缀+哈希）",
          compact_content_seed(raw_seed) == lr1.session_id)


def test_dedupe() -> None:
    print("[dedupe]")
    t1 = [{"role": "user", "content": "第一句"}]
    d = extract_new_content([], t1)
    check("新会话模式", d.is_new_session and d.mode == "new_session", d.to_dict())
    check("新会话转发全部", d.new_text == "第一句", d.new_text)

    stored = history_for_storage(t1)
    d = extract_new_content(stored, t1)
    check("原样重发 -> 没新内容", not d.has_new_content and d.mode == "no_new_content", d.to_dict())

    t2 = t1 + [
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "第二句"},
    ]
    d = extract_new_content(stored, t2)
    check("追加一轮 -> 只转发最新", d.new_text == "第二句" and d.mode == "prefix", d.to_dict())

    stored2 = history_for_storage(t2)
    t3 = t2[:-1] + [{"role": "user", "content": "第二句（改过）"}]
    d = extract_new_content(stored2, t3)
    check("最后一条被改写 -> 转发改写后的内容",
          d.new_text == "第二句（改过）" and d.has_new_content, d.to_dict())

    # 中间一条被改写：公共前缀只能比到改写点之前，锚点兜底要能认出后面没变的老消息
    t4 = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "B"},
        {"role": "user", "content": "C"},
    ]
    t5 = [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "B（改过）"},
        {"role": "user", "content": "C"},
        {"role": "assistant", "content": "D"},
        {"role": "user", "content": "E"},
    ]
    d = extract_new_content(history_for_storage(t4), t5)
    check("中间被改写 -> 锚点兜底只转发真新内容",
          d.mode in ("anchor", "rewritten") and "E" in d.new_text and "C" not in d.new_text, d.to_dict())

    t6 = t2 + [
        {"role": "assistant", "content": "第二个在的"},
        {"role": "user", "content": "还有这条"},
        {"role": "tool", "content": "工具结果"},
    ]
    d = extract_new_content(stored2, t6)
    check("多条新内容合并转发", "还有这条" in d.new_text and "工具结果" in d.new_text, d.new_text)
    check("不带助手回复", "第二个在的" not in d.new_text, d.new_text)

    t8 = [
        {"role": "user", "content": "重复一句"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "重复一句"},
    ]
    d = extract_new_content(history_for_storage(t8[:2]), t8)
    check("用户重复同一句话仍算新内容", d.new_text == "重复一句" and d.has_new_content, d.to_dict())

    t9 = [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "问题"},
          {"role": "assistant", "content": "答案"}, {"role": "user", "content": "追问"}]
    d = extract_new_content(history_for_storage(t9[:3]), t9)
    check("前缀 system 不重复转发", d.new_text == "追问", d.new_text)

    d = extract_new_content(stored2, [])
    check("空请求体不炸", d.mode == "empty" and not d.has_new_content, d.to_dict())

    parts = [{"role": "user", "content": [{"type": "text", "text": "看图"},
                                        {"type": "image_url", "image_url": {"url": "x"}}]}]
    d = extract_new_content([], parts)
    check("多模态内容抽文本", "看图" in d.new_text and "[图片]" in d.new_text, d.new_text)

    check("存储格式只留 role/content",
          history_for_storage(t2) == [
              {"role": "user", "content": "第一句"},
              {"role": "assistant", "content": "在的"},
              {"role": "user", "content": "第二句"},
          ], history_for_storage(t2))


def test_store() -> None:
    print("[store]")
    cfg = load_config()
    # 自测绝不能碰线上会话状态：线上 prefix 是给 serve 用的，自测换独立 prefix，
    # 否则 set_active_session 会把线上的"活跃会话"改掉，害得下一个真实请求误判会话切换、白白重发一次 /clear。
    live_prefix = cfg.redis.prefix.rstrip(":")
    cfg.redis.prefix = live_prefix + "_selftest"
    try:
        store = build_store(cfg)
    except RuntimeError as exc:
        check("store 可用", False, str(exc))
        return
    backend = store.backend
    check("自测用独立 prefix", cfg.redis.prefix != live_prefix, cfg.redis.prefix)
    sid = "selftest-" + str(os.getpid())
    saved_active = store.get_active_session()
    store.delete_session(sid)

    try:
        check("初始未初始化", not store.is_initialized(sid))
        check("初始无历史", store.get_history(sid) == [])

        store.set_history(sid, [{"role": "user", "content": "hi"}])
        check("历史回读", store.get_history(sid) == [{"role": "user", "content": "hi"}])

        store.mark_initialized(sid, source="selftest")
        check("初始化标记", store.is_initialized(sid))
        check("状态含来源", store.get_state(sid).get("source") == "selftest", store.get_state(sid))

        store.update_state(sid, turns=3)
        check("状态累加保留旧字段", int(store.get_state(sid).get("turns")) == 3 and store.is_initialized(sid))

        store.set_last_reply(sid, "回复内容")
        check("回复回读", store.get_last_reply(sid) == "回复内容")

        store.set_active_session(sid)
        check("活跃会话", store.get_active_session() == sid)

        store.set_active_session("别的会话")
        check("清非当前活跃不误伤", store.clear_active_session(sid) is False)
        check("活跃会话保持不动", store.get_active_session() == "别的会话")
        store.set_active_session(sid)
        check("清当前活跃", store.clear_active_session(sid) is True)
        check("活跃会话已清空", store.get_active_session() == "")
        store.set_active_session(sid)

        check("抢初始化锁", store.try_acquire_init_lock(sid, ttl=5))
        check("锁不可重入", not store.try_acquire_init_lock(sid, ttl=5))
        store.release_init_lock(sid)
        check("锁可释放", store.try_acquire_init_lock(sid, ttl=5))
        store.release_init_lock(sid)

        listed = {item["session_id"]: item for item in store.list_sessions()}
        check("会话可列出", sid in listed, list(listed)[:3])
        if sid in listed:
            check("列出含初始化标记", bool(listed[sid]["initialized"]))
            check("列出含轮次", int(listed[sid]["turns"]) == 3)

        store.delete_session(sid)
        check("删除后无历史", store.get_history(sid) == [])
        check("删除后无状态", not store.get_state(sid))
    finally:
        store.delete_session(sid)
        store.clear_active_session(sid)
        if saved_active:
            store.set_active_session(saved_active)
        store.close()
    print(f"  (后端: {backend}，prefix: {cfg.redis.prefix})")


def test_rhythm() -> None:
    """拟人节流：默认必须是关的，且环境变量单项覆盖不能把它偷偷加回来。"""
    print("[rhythm]")
    from wechat2api.config import Config
    from wechat2api.wechat_client import WeChatClient

    try:
        from wechatauto import rhythm
    except ImportError as exc:  # 没装库就没法验，报出来而不是假装通过
        check("wechatauto 可导入", False, exc)
        return

    saved = rhythm.profile().name
    saved_env = {
        k: os.environ.pop(k, None)
        for k in ("WECHATAUTO_RHYTHM", "WECHATAUTO_WRITE_GAP", "WECHATAUTO_WRITE_BURST")
    }
    try:
        cfg = Config()
        check("默认档位为 off", cfg.wechat.rhythm == "off", cfg.wechat.rhythm)

        WeChatClient(cfg)._apply_rhythm()
        check("客户端把档位套成 off", rhythm.profile().name == "off", rhythm.profile().name)
        check("off 档写动作不等待", rhythm.gate("selftest") == 0.0)
        check("off 档无写上限", rhythm.profile().burst <= 0, rhythm.profile().burst)

        # 环境变量单项覆盖是"切档后仍生效"的，关掉节流时必须一起作废
        os.environ["WECHATAUTO_WRITE_GAP"] = "5"
        rhythm.configure(gap=(2.5, 6.0), burst=6)  # 模拟库默认 natural 档
        check("已模拟出写间隔", rhythm.profile().gap[1] > 0)
        WeChatClient(cfg)._apply_rhythm()
        check("已作废 WECHATAUTO_WRITE_GAP", "WECHATAUTO_WRITE_GAP" not in os.environ)
        check("作废后间隔归零", rhythm.profile().gap[1] <= 0, rhythm.profile().gap)
        check("作废后写动作仍不等待", rhythm.gate("selftest") == 0.0)

        # 显式开回来：配置/环境变量都能切换，未知档位安全退回 off
        os.environ["WECHAT2API__WECHAT__RHYTHM"] = "natural"
        on_cfg = load_config()
        check("env 可覆盖档位", on_cfg.wechat.rhythm == "natural", on_cfg.wechat.rhythm)
        WeChatClient(on_cfg)._apply_rhythm()
        check("natural 档有写间隔", rhythm.profile().gap[1] > 0, rhythm.profile().gap)

        on_cfg.wechat.rhythm = "没这个档"
        WeChatClient(on_cfg)._apply_rhythm()
        check("未知档位退回 off", rhythm.profile().name == "off", rhythm.profile().name)

        on_cfg.wechat.rhythm = ""
        WeChatClient(on_cfg)._apply_rhythm()
        check("空串不干预库默认", rhythm.profile().name == "off", rhythm.profile().name)
    finally:
        os.environ.pop("WECHAT2API__WECHAT__RHYTHM", None)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        rhythm.set_profile(saved)


def main() -> int:
    test_session_id()
    test_dedupe()
    test_store()
    test_rhythm()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
