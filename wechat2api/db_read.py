"""公众号 / 服务号消息读取（``message/biz_message_N.db``）。

wechatauto 的 ``get_messages`` / ``Chat.GetNewMessage`` 只扫
``message/message_N.db``；而公众号、服务号（``gh_xxx``、``newsapp`` 之类）的消息
实际落在 ``message/biz_message_N.db``。所以对「微信支付」这种服务号目标，只走库自带
的读库路径会一直读到 0 条（会话列表里明明有摘要）。

这里复用 wechatauto 已经解好的连接（``WeChatDB._open``）和正文解码
（``WeChatDB._friendly_content`` / ``_msg_type_name``），只补上：分片定位、
``sort_seq`` 增量、发送者判定（按本分片自己的 ``Name2Id``，见上游 issue #34）。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional

BIZ_SHARD_RE = re.compile(r"^message[\\/]biz_message_\d+\.db$")

_COLS = ("local_id, local_type, create_time, sort_seq, real_sender_id, "
         "message_content, compress_content")


def table_for(user: str) -> str:
    """会话 username -> 消息表名（微信 4.x 用 md5(username) 分表）。"""
    return "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest()


def shard_label(rel: str) -> str:
    return rel.replace(os.sep, "__").replace("/", "__")


def biz_shards(db: Any) -> List[str]:
    """当前所有 biz 消息分片（运行中可能新增，所以每次重扫）。"""
    try:
        db._refresh_db_files()
    except Exception:
        pass
    out = []
    for rel, _path, _ in getattr(db, "_db_files", []) or []:
        if BIZ_SHARD_RE.match(rel.replace(os.sep, "/")):
            out.append(rel)
    return sorted(out)


def _name2id(conn: Any) -> Dict[int, str]:
    try:
        return {int(rid): (u or "") for rid, u in conn.execute(
            "SELECT rowid, user_name FROM Name2Id")}
    except Exception:
        return {}


def _has_table(conn: Any, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _decode(db: Any, row: Any, names: Dict[int, str], self_wxid: str,
            shard: str) -> Optional[dict]:
    from wechatauto.db import WeChatDB

    local_type = row["local_type"]
    mtype = WeChatDB._msg_type_name(local_type)
    content = row["message_content"]
    if isinstance(content, (bytes, bytearray, memoryview)):
        content = WeChatDB._friendly_content(bytes(content), mtype)
    placeholder = "[%s]" % mtype
    if not isinstance(content, str):
        content = placeholder
    if not content.strip():
        content = placeholder
    if content == placeholder and row["compress_content"]:
        cc = row["compress_content"]
        if isinstance(cc, (bytes, bytearray, memoryview)):
            cc_text = WeChatDB._friendly_content(bytes(cc), mtype)
            if cc_text != placeholder:
                content = cc_text
    try:
        sender = names.get(int(row["real_sender_id"] or 0), "") or ""
    except (TypeError, ValueError):
        sender = ""
    return {
        "local_id": row["local_id"],
        "local_type": local_type,
        "type": mtype,
        "sender_username": sender,
        "is_self": bool(self_wxid) and sender == self_wxid,
        "create_time": row["create_time"],
        "sort_seq": row["sort_seq"],
        "content": content,
        "shard": shard,
    }


def read_biz_rows(db: Any, user: str, limit: int = 50,
                  since_seq: Optional[int] = None,
                  self_wxid: str = "") -> List[dict]:
    """读某会话的 biz 消息。

    since_seq 为 None：取最近 ``limit`` 条（``sort_seq`` 升序返回）；
    否则只返回 ``sort_seq > since_seq`` 的行（升序），用于增量轮询。
    """
    table = table_for(user)
    out: List[dict] = []
    for rel in biz_shards(db):
        try:
            conn = db._open(rel)
        except Exception:
            continue
        try:
            if not _has_table(conn, table):
                continue
            names = _name2id(conn)
            label = shard_label(rel)
            if since_seq is None:
                sql = (f"SELECT {_COLS} FROM {table} "
                       "ORDER BY sort_seq DESC, local_id DESC LIMIT ?")
                params: tuple = (max(1, int(limit)),)
            else:
                sql = (f"SELECT {_COLS} FROM {table} WHERE sort_seq > ? "
                       "ORDER BY sort_seq ASC, local_id ASC")
                params = (int(since_seq),)
            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception:
                continue
            for row in rows:
                item = _decode(db, row, names, self_wxid, label)
                if item is not None:
                    out.append(item)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    out.sort(key=lambda r: (r["sort_seq"], r["local_id"]))
    if since_seq is None:
        out = out[-max(1, int(limit)):]
    return out


def latest_biz_seq(db: Any, user: str) -> int:
    """该会话当前最大 ``sort_seq``（没有则 0），用于建立收信水位。"""
    rows = read_biz_rows(db, user, limit=1)
    return int(rows[-1]["sort_seq"]) if rows else 0
