"""实机微信连通性验证（会真的操作微信窗口，谨慎使用）。

用法::

    .venv\\Scripts\\python.exe tools\\probe_real.py read            # 读最近消息（只读）
    .venv\\Scripts\\python.exe tools\\probe_real.py send 你好        # 真发一条（不等回复）
    .venv\\Scripts\\python.exe tools\\probe_real.py wait 60         # 等 60 秒看有没有人来消息
    .venv\\Scripts\\python.exe tools\\probe_real.py init            # 真跑一遍 /clear + prompt.txt

先跑 read 确认读得到，再跑 send 确认发得出，最后才用 init。

读历史时会把公众号/服务号分片（``message/biz_message_N.db``）一起读出来：
普通会话正文在 ``message_N.db``，而「微信支付」这类目标的正文只在 biz 分片里，
只走上游 ``Chat.GetAllMessage`` 会是 0 条。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat2api import db_read  # noqa: E402
from wechat2api.config import load_config  # noqa: E402
from wechat2api.wechat_client import WeChatClient, WeChatHookError  # noqa: E402


def _history(client: WeChatClient, limit: int) -> list[dict]:
    """普通会话 + biz 分片合并后的历史（按时间升序）。"""
    out: list[dict] = []
    chat = client._chat
    try:
        for msg in chat.GetAllMessage() or []:
            out.append({
                "attr": str(getattr(msg, "attr", "") or ""),
                "who": str(getattr(msg, "sender", "") or getattr(msg, "sender_wxid", "") or ""),
                "create_time": getattr(msg, "create_time", None),
                "content": str(getattr(msg, "content", "") or ""),
                "shard": "message",
            })
    except Exception as exc:
        print(f"  (普通分片读失败: {exc!r})")
    if client._wx is not None and client.target_wxid:
        try:
            for row in db_read.read_biz_rows(
                client._wx._db, client.target_wxid, limit=max(limit, 50),
                self_wxid=client.self_wxid,
            ):
                out.append({
                    "attr": "self" if row.get("is_self") else "friend",
                    "who": str(row.get("sender_username") or ""),
                    "create_time": row.get("create_time"),
                    "content": str(row.get("content") or ""),
                    "shard": "biz",
                })
        except Exception as exc:
            print(f"  (biz 分片读失败: {exc!r})")
    out.sort(key=lambda r: (float(r["create_time"] or 0), r["attr"]))
    return out


def _dump(rows: list[dict], limit: int, header: str) -> None:
    print(f"--- {header}（共 {len(rows)} 条，显示后 {limit} 条）---")
    for row in rows[-limit:]:
        text = str(row["content"]).replace("\n", "\\n")
        if len(text) > 120:
            text = text[:120] + "…"
        when = ""
        ts = row["create_time"]
        if isinstance(ts, (int, float)) and ts > 0:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        attr = str(row["attr"] or row["shard"])[:6]
        print(f"  [{attr:6}] {when:>14} {str(row['who'])[:24]:<24} | {text}")


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "read"
    cfg = load_config()
    client = WeChatClient(cfg)
    try:
        client.ensure()
        print(f"已连接: 本机={client.self_wxid} 目标={client.target}({client.target_wxid})")

        if mode == "read":
            limit = int(argv[1]) if len(argv) > 1 else 20
            _dump(_history(client, limit), limit, f"{client.target} 历史消息")
            return 0

        if mode == "send":
            text = " ".join(argv[1:]) or "wechat2api 连通性测试"
            client.sync_baseline()
            client.send_text(text)
            print(f"已发送: {text!r}")
            time.sleep(1.5)
            _dump(_history(client, 5), 5, "发送后的尾巴")
            return 0

        if mode == "wait":
            timeout = float(argv[1]) if len(argv) > 1 else 60.0
            client.sync_baseline()
            print(f"等 {timeout:.0f} 秒（现在去给这个微信发条消息试试）…")
            reply = client.wait_for_reply(timeout=timeout, settle=2.0)
            print(f"收到回复: {reply!r}" if reply else "超时，什么都没收到")
            return 0 if reply else 1

        if mode == "init":
            report = client.initialize_session("probe", cfg.prompt_text(),
                                               command=cfg.wechat.init_command)
            print(f"ok={report.ok} error={report.error!r}")
            print(f"  /clear  -> {report.command_reply!r}")
            print(f"  prompt  -> {report.prompt_reply!r}（{report.prompt_chars} 字）")
            return 0 if report.ok else 2

        print(f"未知模式: {mode}")
        return 1
    except WeChatHookError as exc:
        print(f"失败: {exc}")
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
