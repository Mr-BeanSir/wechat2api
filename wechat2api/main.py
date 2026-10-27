"""命令行入口: ``python -m wechat2api`` / ``wechat2api``。

    python -m wechat2api serve                 # 起服务（默认）
    python -m wechat2api probe                 # 只体检：连微信、热激活 UIA、读目标会话
    python -m wechat2api send "hello"          # 直接发一条，验证发送链路
    python -m wechat2api status                # 打印存储/活跃会话/微信状态
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import DEFAULT_CONFIG_FILE, Config, load_config

log = logging.getLogger("wechat2api")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("wechatauto").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat2api", description="实机微信 -> OpenAI 兼容接口")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG_FILE), help="配置文件路径")
    parser.add_argument("--log-level", default=os.environ.get("WECHAT2API_LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="改代码自动重载（调试用）")

    sub.add_parser("probe", help="连接体检（连微信 + 热激活 + 读目标会话）")
    sub.add_parser("status", help="打印状态")

    send = sub.add_parser("send", help="直接发一条文本（跳过会话/去重逻辑）")
    send.add_argument("text", nargs="+")
    send.add_argument("--no-wait", action="store_true", help="不等回复")

    return parser


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    log.info("监听 http://%s:%d/v1  （模型名 %s）", host, port, cfg.server.model_name)
    uvicorn.run(
        create_app(cfg),
        host=host,
        port=port,
        log_level=str(args.log_level).lower(),
        reload=bool(args.reload),
    )
    return 0


def cmd_probe(cfg: Config, _: argparse.Namespace) -> int:
    from .store import build_store
    from .wechat_client import WeChatClient, WeChatHookError

    store = build_store(cfg)
    print(f"[存储] {store.backend}  ping={store.ping()}")
    print(f"[目标] {cfg.wechat.target}   初始化= {cfg.wechat.init_command} + {cfg.prompt_path}")
    try:
        prompt = cfg.prompt_text()
        print(f"[提示词] {len(prompt)} 字: {prompt[:60]!r}")
    except FileNotFoundError as exc:
        print(f"[提示词] 读不到: {exc}")

    client = WeChatClient(cfg)
    try:
        client.ensure()
        print("[微信] 已连接")
        print(json.dumps(client.status(), ensure_ascii=False, indent=2))
    except WeChatHookError as exc:
        print(f"[微信] 连接失败: {exc}")
        return 2
    finally:
        client.close()
        store.close()
    return 0


def cmd_status(cfg: Config, _: argparse.Namespace) -> int:
    from .store import build_store
    from .wechat_client import WeChatClient

    store = build_store(cfg)
    print(f"[存储] {store.backend}")
    print(f"[活跃会话] {store.get_active_session() or '(空)'}")
    for item in store.list_sessions():
        print(
            f"  - {item['session_id'][:32]:<32} init={int(item['initialized'])} "
            f"turns={item['turns']:<4} hist={item['history_len']:<4} reply={item['last_reply'][:30]!r}"
        )
    client = WeChatClient(cfg)
    try:
        print(json.dumps(client.status(), ensure_ascii=False, indent=2))
    finally:
        client.close()
        store.close()
    return 0


def cmd_send(cfg: Config, args: argparse.Namespace) -> int:
    from .wechat_client import WeChatClient, WeChatHookError

    text = " ".join(args.text)
    client = WeChatClient(cfg)
    try:
        if args.no_wait:
            client.sync_baseline()
            client.send_text(text)
            print("已发送（没等回复）")
        else:
            reply = client.send_and_wait(text)
            print(f"回复: {reply}")
    except WeChatHookError as exc:
        print(f"失败: {exc}")
        return 2
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"配置有问题: {exc}", file=sys.stderr)
        return 1

    command = args.command or "serve"
    if command == "serve":
        return cmd_serve(cfg, args)
    if command == "probe":
        return cmd_probe(cfg, args)
    if command == "status":
        return cmd_status(cfg, args)
    if command == "send":
        return cmd_send(cfg, args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
