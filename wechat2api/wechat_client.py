"""实机微信适配层。

只做三件事：**连接**(启动/热激活 UIA)、**读**(收回复)、**写**(发消息)。
业务（会话判定、去重、初始化）都在 pipeline 里，方便单独调试。

关于"hook 实机 wx":
    微信 4.x 主界面是 Qt 自绘，默认不向 UIA 暴露控件树。``wechatauto`` 通过
    在 ``Weixin.dll`` 里写入 Qt 无障碍开关那一个字节（``uia_driver`` 的
    ``ensure_materialized``）来"打开"无障碍树 —— 这是内存补丁式 hook，
    不需要注入 DLL、不需要版本偏移表。打上之后 UIA 才能读到会话列表/输入框；
    补丁随进程生命周期失效（微信重启/升级后控件树会"消失"一次），
    所以这里每次用 GUI 前都会 ``_get_uia()`` 自愈一次。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import db_read
from .config import Config

log = logging.getLogger("wechat2api.wechat")


class WeChatHookError(RuntimeError):
    """连接/发送/收信失败。"""


@dataclass
class Incoming:
    text: str
    nickname: str = ""
    who: str = ""
    is_self: bool = False
    ts: float = 0.0
    msg_id: str = ""

    def identity(self) -> tuple[str, str]:
        return (self.who or self.nickname, self.text.strip())


@dataclass
class InitReport:
    session_id: str
    command: str = ""
    command_reply: str = ""
    prompt_chars: int = 0
    prompt_reply: str = ""
    ok: bool = False
    error: str = ""
    steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "command": self.command,
            "command_reply": self.command_reply,
            "prompt_chars": self.prompt_chars,
            "prompt_reply": self.prompt_reply,
            "ok": self.ok,
            "error": self.error,
            "steps": self.steps,
        }


class WeChatClient:
    """一路串行化的微信读写通道（微信只有一个账号，也只有一个编辑框）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._wx: Any = None
        self._chat: Any = None
        self._self_wxid: str = ""
        self._target_wxid: str = ""
        self._queue: "queue.Queue[Incoming]" = queue.Queue()
        self._listener_on = False
        self._baseline_done = False
        self._biz_seq: int = 0
        self._last_activity: float = 0.0
        self._hook_info: dict = {}

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    @property
    def target(self) -> str:
        return self.cfg.wechat.target

    @property
    def self_wxid(self) -> str:
        """本机账号 wxid（连上后才有值）。"""
        return self._self_wxid or ""

    @property
    def target_wxid(self) -> str:
        """目标会话的 wxid（显示名解析不到时为空）。"""
        return self._target_wxid or ""

    def ensure(self) -> Any:
        """确保已经连上微信（惰性，可重复调用）。"""
        with self._lock:
            if self._chat is not None:
                return self._chat
            try:
                from wechatauto import Chat, WeChat
                from wechatauto.logger import wxlog
            except ImportError as exc:  # pragma: no cover
                raise WeChatHookError(
                    "没装 wechatauto，请先: .venv\\Scripts\\python.exe -m pip install -e D:\\Development\\wechatauto-replica"
                ) from exc

            try:
                wxlog.set_debug(False)
            except Exception:
                pass

            self._apply_rhythm()
            log.info("正在连接实机微信（WeChat 4.x）…")
            self._wx = WeChat()  # 打开主窗口、解密并读取本地 SQLCipher 库
            self._self_wxid = (self._wx._db.get_self_info() or {}).get("username", "")
            self._activate_uia()
            self._chat = self._make_chat(Chat)
            self._start_listener()
            self._baseline_done = False
            log.info(
                "已连接: 本机微信=%s 目标会话=%s(%s)",
                self._self_wxid or "?",
                self.target,
                self._target_wxid or "显示名",
            )
            return self._chat

    # ------------------------------------------------------------------
    # 拟人节流（wechatauto.rhythm）
    # ------------------------------------------------------------------
    def _apply_rhythm(self) -> None:
        """把 ``wechat.rhythm`` 档位套到 wechatauto 的拟人节流上。

        wechatauto 的默认档是 ``natural``：两次"对外可见的写动作"之间至少要隔
        2.5~6s（距上次写不足才补等，够了就不等）、120s 内写超过 6 次再冷却
        30~75s、点击落点随机化、光标走曲线、拼⾳回退逐字停 0.05~0.16s。
        我们默认 ``off``：只留控件定位/渲染必需的等待，不去补写间隔。
        想恢复拟人节奏，把 ``wechat.rhythm`` 改成 ``natural`` / ``calm`` 即可。
        """
        name = (self.cfg.wechat.rhythm or "").strip().lower()
        try:
            from wechatauto import rhythm
        except ImportError:  # 库没装时 ensure() 后面会给出安装提示
            return
        if not name:
            log.info("拟人节流: 不干预，沿用 wechatauto 默认档")
            return
        if name not in rhythm.PROFILES:
            log.warning("未知拟人节流档位 %r，按 off 处理", name)
            name = "off"
        try:
            # 这两个环境变量是"切档之后仍然生效"的单项覆盖，关节流时必须一起
            # 作废，否则 WECHATAUTO_WRITE_GAP 会把写动作间隔又加回来。
            stale = [
                k for k in ("WECHATAUTO_WRITE_GAP", "WECHATAUTO_WRITE_BURST")
                if os.environ.pop(k, None) is not None
            ]
            prof = rhythm.set_profile(name)
            if name == "off":
                # 双保险：即便别处套过单项覆盖，也让 gate() 直接短路返回 0。
                prof = rhythm.configure(gap=(0.0, 0.0), burst=0)
        except Exception as exc:
            log.warning("设置拟人节流档位失败（沿用库默认）: %r", exc)
            return
        log.info(
            "拟人节流档位: %s（写间隔 %.1f~%.1fs，%ds 上限 %d 次，字间 %.2f~%.2fs）%s",
            prof.name, prof.gap[0], prof.gap[1], int(prof.window), prof.burst,
            prof.type_gap[0], prof.type_gap[1],
            ("；已忽略环境变量 " + ", ".join(stale)) if stale else "",
        )

    def _activate_uia(self) -> None:
        """热激活 Qt 无障碍门（写 Weixin.dll 那一个字节），失败不致命（会退 OCR）。"""
        gui = getattr(self._wx, "_gui", None)
        info: dict = {"materialized": None, "error": ""}
        if gui is None:
            self._hook_info = info
            return
        try:
            getter = getattr(gui, "_get_uia", None)
            if callable(getter):
                uia = getter(refresh=True)
                info["materialized"] = bool(uia is not None)
            else:  # 老版本兼容
                uia = getattr(gui, "_uia", None)
                info["materialized"] = bool(uia is not None)
            info["hwnd"] = getattr(gui, "main_hwnd", None) or getattr(gui, "hwnd", None)
        except Exception as exc:
            info["error"] = repr(exc)
            log.warning("UIA 热激活失败（将退回坐标/OCR 路径）: %r", exc)
        self._hook_info = info
        log.info("UIA 热激活结果: %s", info)

    def _make_chat(self, ChatCls) -> Any:
        """构造目标会话对象；服务号（如"微信支付"）不在联系人表里，
        用会话列表反查真实 username，否则读库会读不到东西。"""
        wx = self._wx
        chat = ChatCls(self.target, wx._gui, wx._db)
        wxid = ""
        try:
            for item in wx.GetSession() or []:
                name = getattr(item, "name", "") or ""
                if name == self.target:
                    wxid = getattr(item, "username", "") or ""
                    break
        except Exception as exc:
            log.debug("从会话列表反查 username 失败: %r", exc)
        if not wxid:
            try:
                wxid = wx._db.username_by_nickname(self.target) or ""
            except Exception:
                wxid = ""
        if not wxid:
            wxid = getattr(chat, "_wxid", "") or ""
        self._target_wxid = wxid
        if wxid:
            chat._wxid = wxid  # 让 GetNewMessage/GetAllMessage 用真实 username 读库
        return chat

    def _start_listener(self) -> None:
        """全局监听兜底：DB-seq 轮询读不到时，靠回调推消息。"""
        if self._listener_on:
            return
        try:
            self._wx.AddListenAll(self._on_all_message)
            self._wx.StartListening()
            self._listener_on = True
            log.debug("已开启全局监听")
        except Exception as exc:
            log.warning("开启全局监听失败（继续用轮询收信）: %r", exc)

    def _on_all_message(self, msg: Any, chat: Any) -> None:
        try:
            who = getattr(chat, "who", "") or ""
            nickname = getattr(chat, "nickname", "") or ""
            if not self._is_target(who, nickname):
                return
            self._queue.put(
                Incoming(
                    text=str(getattr(msg, "content", "") or ""),
                    nickname=nickname,
                    who=who,
                    is_self=bool(getattr(msg, "is_self", False)),
                    ts=time.time(),
                    msg_id=str(getattr(msg, "id", "") or ""),
                )
            )
        except Exception as exc:  # pragma: no cover
            log.debug("监听回调处理失败: %r", exc)

    def _is_target(self, who: str, nickname: str) -> bool:
        if self._target_wxid and who and who == self._target_wxid:
            return True
        if nickname and nickname == self.target:
            return True
        return bool(who) and who == self.target

    def close(self) -> None:
        with self._lock:
            if self._wx is not None:
                try:
                    if self._listener_on:
                        self._wx.StopListening()
                except Exception:
                    pass
            self._wx = None
            self._chat = None
            self._listener_on = False

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        """发一条文本给目标会话（抛 WeChatHookError 表示失败）。"""
        if not text or not text.strip():
            return
        with self._lock:
            chat = self.ensure()
            try:
                result = chat.SendMsg(text, who=self.target, exact=self.cfg.wechat.exact)
            except Exception as exc:
                raise WeChatHookError(f"发送失败: {exc!r}") from exc
            self._last_activity = time.time()
            if not _ok(result):
                raise WeChatHookError(f"发送失败: {_describe(result)}")
            log.debug("已发送 %d 字 -> %s", len(text), self.target)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _drain_queue(self) -> list[Incoming]:
        out: list[Incoming] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def _poll_db(self) -> list[Incoming]:
        """DB 增量轮询（Chat.GetNewMessage 自己维护 seq 水位）。"""
        out: list[Incoming] = []
        chat = self._chat
        if chat is not None:
            try:
                rows = chat.GetNewMessage() or []
            except Exception as exc:
                log.debug("GetNewMessage 失败: %r", exc)
                rows = []
            for msg in rows:
                try:
                    if bool(getattr(msg, "is_self", False)):
                        continue
                    text = str(getattr(msg, "content", "") or "")
                    if not text.strip():
                        continue
                    out.append(
                        Incoming(
                            text=text,
                            nickname=self.target,
                            who=self._target_wxid,
                            is_self=False,
                            ts=time.time(),
                            msg_id=str(getattr(msg, "id", "") or ""),
                        )
                    )
                except Exception:
                    continue
        out.extend(self._poll_biz())
        return out

    def _poll_biz(self) -> list[Incoming]:
        """公众号/服务号增量（消息落在 ``message/biz_message_N.db``）。

        上游 ``Chat.GetNewMessage`` 只扫 ``message_N.db``，对「微信支付」这类
        服务号永远读到 0 条：摘要明明在会话列表里，消息正文却在另一个分片库里。
        这里自己维护一个 ``sort_seq`` 水位，复用已解密的连接读正文。
        """
        user = self._target_wxid
        if not user or self._wx is None:
            return []
        if not self._baseline_done:
            # 还没建水位：先把历史读掉只当水位，别把旧消息当新回复
            try:
                self._biz_seq = db_read.latest_biz_seq(self._wx._db, user)
            except Exception as exc:
                log.debug("建立 biz 收信水位失败: %r", exc)
            return []
        try:
            rows = db_read.read_biz_rows(
                self._wx._db, user, since_seq=self._biz_seq,
                self_wxid=self._self_wxid,
            )
        except Exception as exc:
            log.debug("读 biz 分片失败: %r", exc)
            return []
        out: list[Incoming] = []
        for row in rows:
            try:
                seq = int(row.get("sort_seq") or 0)
                if seq > self._biz_seq:
                    self._biz_seq = seq
                if row.get("is_self"):
                    continue
                text = str(row.get("content") or "")
                if not text.strip():
                    continue
                out.append(
                    Incoming(
                        text=text,
                        nickname=self.target,
                        who=user,
                        is_self=False,
                        ts=time.time(),
                        msg_id=str(row.get("local_id") or ""),
                    )
                )
            except Exception:
                continue
        return out

    def sync_baseline(self) -> None:
        """把 seq 水位推到"现在"，避免把上一轮/历史消息当成新回复。"""
        with self._lock:
            chat = self.ensure()
            self._drain_queue()
            if not self._baseline_done:
                try:
                    chat.GetNewMessage()  # 第一次调用只设水位，固定返回 []
                except Exception as exc:
                    log.debug("建立收信水位失败: %r", exc)
                self._baseline_done = True
            if self._wx is not None and self._target_wxid:
                try:
                    self._biz_seq = db_read.latest_biz_seq(self._wx._db, self._target_wxid)
                except Exception as exc:
                    log.debug("建立 biz 收信水位失败: %r", exc)

    def wait_for_reply(self, timeout: Optional[float] = None,
                       settle: Optional[float] = None) -> str:
        """等一条（或一段连续的）回复。

        settle: 收到最后一条后静默这么久就认为对方说完了（多气泡回复）。
        """
        cfg = self.cfg.wechat
        timeout = float(timeout if timeout is not None else cfg.reply_timeout)
        settle = float(settle if settle is not None else cfg.reply_settle)
        poll = max(0.1, float(cfg.poll_interval))
        deadline = time.time() + timeout

        chunks: list[str] = []
        seen: set[tuple[str, str]] = set()
        last_hit = 0.0

        while time.time() < deadline:
            batch = self._drain_queue() + self._poll_db()
            for item in batch:
                if item.is_self:
                    continue
                text = item.text.strip()
                if not text:
                    continue
                key = item.identity()
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(text)
                last_hit = time.time()
                log.debug("收到回复片段: %s", text[:80])
            if last_hit and (time.time() - last_hit) >= settle:
                break
            time.sleep(poll)

        self._last_activity = time.time()
        return "\n".join(chunks)

    # ------------------------------------------------------------------
    # 组合
    # ------------------------------------------------------------------
    def send_and_wait(self, text: str, timeout: Optional[float] = None,
                      settle: Optional[float] = None, require_reply: bool = True) -> str:
        with self._lock:
            self.sync_baseline()
            self.send_text(text)
            reply = self.wait_for_reply(timeout=timeout, settle=settle)
            if require_reply and not reply.strip():
                raise WeChatHookError(f"发出内容后 {timeout or self.cfg.wechat.reply_timeout}s 内没等到回复")
            return reply

    def initialize_session(self, session_id: str, prompt_text: str,
                           command: Optional[str] = None) -> InitReport:
        """新会话初始化: 先发 /clear 等回复，再发 prompt.txt 内容等回复。"""
        cfg = self.cfg.wechat
        report = InitReport(session_id=session_id, command=command or cfg.init_command)
        require = bool(cfg.init_require_reply)

        if report.command:
            try:
                reply = self.send_and_wait(
                    report.command,
                    timeout=cfg.init_command_timeout,
                    require_reply=require,
                )
                report.command_reply = reply
                report.steps.append({"step": "clear", "ok": True, "reply": reply})
            except WeChatHookError as exc:
                report.steps.append({"step": "clear", "ok": False, "error": str(exc)})
                report.error = f"{report.command} 失败: {exc}"
                if require:
                    return report
                log.warning("%s", report.error)

        body = (prompt_text or "").strip()
        if body:
            try:
                reply = self.send_and_wait(
                    body,
                    timeout=cfg.init_prompt_timeout,
                    require_reply=require,
                )
                report.prompt_chars = len(body)
                report.prompt_reply = reply
                report.steps.append({"step": "prompt", "ok": True, "reply": reply})
            except WeChatHookError as exc:
                report.prompt_chars = len(body)
                report.steps.append({"step": "prompt", "ok": False, "error": str(exc)})
                report.error = f"发送 prompt.txt 失败: {exc}"
                if require:
                    return report
                log.warning("%s", report.error)

        report.ok = not report.error
        return report

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> dict:
        gui = getattr(self._wx, "_gui", None) if self._wx is not None else None
        connected = self._chat is not None
        last_reply = ""
        try:
            if connected:
                msgs = self._chat.GetAllMessage() or []
                if msgs:
                    last_reply = str(getattr(msgs[-1], "content", "") or "")
        except Exception:
            pass
        if not last_reply and connected and self._wx is not None and self._target_wxid:
            # 服务号/公众号：正文只在 biz 分片里
            try:
                rows = db_read.read_biz_rows(
                    self._wx._db, self._target_wxid, limit=1,
                    self_wxid=self._self_wxid,
                )
                if rows:
                    last_reply = str(rows[-1].get("content") or "")
            except Exception:
                pass
        return {
            "connected": connected,
            "self_wxid": self._self_wxid,
            "target": self.target,
            "target_wxid": self._target_wxid,
            "listener": self._listener_on,
            "hook": dict(self._hook_info),
            "main_hwnd": getattr(gui, "main_hwnd", None) or getattr(gui, "hwnd", None) if gui else None,
            "last_activity": self._last_activity,
            "last_message_preview": last_reply[:120],
            "biz_seq": self._biz_seq,
            "queue_size": self._queue.qsize(),
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _ok(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, dict):
        if "status" in result:
            return str(result.get("status")) == "成功"
        return True
    flag = getattr(result, "is_success", None)
    if flag is not None:
        try:
            return bool(flag() if callable(flag) else flag)
        except Exception:
            return True
    return bool(result) if isinstance(result, bool) else True


def _describe(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("message") or result)
    return str(result)


__all__ = ["WeChatClient", "WeChatHookError", "Incoming", "InitReport"]
