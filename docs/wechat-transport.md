# 技术实现：wechat2api 如何与实机微信收发消息

本文说明 `wechat2api` 把 OpenAI 兼容的 HTTP 接口接到**真实登录的微信客户端**上的完整实现：
收信怎么来、发信怎么落、失败怎么办、以及哪些地方是版本敏感的脆点。

代码引用格式 `文件:行号`，路径相对两个仓库根：

- `wechatauto/…` → wechatauto-replica 仓库里的 `wechatauto/`（底层自动化库，实测 v1.2.8.1）
- `wechat2api/…` → 本仓库里的 `wechat2api/`（适配层）

---

## 0. 结论先行

| 问题 | 答案 |
| --- | --- |
| 走的是什么通道 | **不是** hook/注入协议。是「解密本地 SQLCipher 库读消息」+「UIA/坐标操作真实窗口发消息」 |
| 为什么不用 wxhelper / WeChatFerry | 两者只支持 3.9.x 家族；本机是 Weixin **4.1.15.56**，进程族与消息库结构都不同 |
| 需要注入 DLL 吗 | 不需要。DB 密钥靠**读目标进程内存**提取；UIA 靠**热写运行中进程 1 个字节**打开 Qt 无障碍门 |
| 收信机制 | 库轮询 + `sort_seq` 水位（`AddListenAll` 本质也是库轮询，不是微信推送） |
| 发信机制 | 剪贴板 `Ctrl+V` 写入输入框 → 回读比对 → 回车提交 |
| 最大脆点 | 版本敏感的 RVA / 内存偏移常量；微信升级后 UIA 热激活可能失效（会降级 OCR/坐标） |

---

## 1. 运行前提（不满足就直接失败）

| 前提 | 原因 | 证据 |
| --- | --- | --- |
| **64 位 Python** | 32 位进程枚举不到 64 位 `Weixin.exe` 的模块，DB 密钥扫不出来 | `db.py:1091`、`accessibility.py:606-609` |
| 与微信**同级权限**（普通用户即可，不要管理员） | 需要 `OpenProcess(VM_READ\|QUERY_INFORMATION)` | `db.py:1288-1303` |
| 微信已登录且**未锁屏**、窗口未最小化 | 发信要真实点击/按键 | `guia.py:31`、`guia.py:2099-2100` |
| 微信进程与多开实例中**目标主窗一致** | 否则拒绝借用坐标 | `guia.py:1735-1737`、`uia_driver.py:217` |

本项目在 `WeChatClient.ensure()`（`wechat2api/wechat_client.py:106-138`）里按顺序做：
导入库 → 关调试日志 → **套节流档位**（见 §5）→ `WeChat()` 打开主窗口并解密读库 →
取 `self_wxid` → **热激活 UIA** → 构造目标会话对象 → 开监听 → 建立收信基线。

---

## 2. 收信：从密文库到一段回复

### 2.1 数据库怎么变成可读的

```
Weixin.exe 内存扫描 → master key
        ↓ PBKDF2-HMAC-SHA512(master, page1[:16], 256000)   （db.py:584）
        ↓ AES-CBC 逐页解密（页 4096，保留 80 字节 = IV16 + HMAC64）  （db.py:165-186, 363-375）
整库明文临时文件 <temp>/wechatauto_db/<account>/…     （db.py:512-514）
        ↓ 标准 sqlite3 以 mode=ro 打开                  （db.py:1483-1485）
```

要点：

- 密钥**不是**配置项、也没有固定算法：靠以 `com.Tencent.WCDB.Config.Cipher` 为锚走指针链、XOR 解掩码后提取 hex（`db.py:46, 885-896, 1090-1170`）。所以密钥只能"同权限进程内存扫描 + 结果缓存"。
- 缓存失效用 `.stamp` 记录「版本 + mtime + size + WAL mtime/size」，任一变化即重建；解密撕裂重试 4 次，失败退上一份可用副本，损坏则清缓存重来（`db.py:1402-1419, 1441-1455, 1595-1633, 1648-1691`）。库文件名列表不变时直接早退，不做重复解密。
- 正文长文本是 zstd 压缩的，读库时解压（`db.py:2204-2217`）。
- 微信升级**不会**重新加密历史库，所以密钥缓存不受升级影响（`db.py:1211-1215`）。

### 2.2 消息水位：`sort_seq`

消息表按 `Msg_<md5(username)>` 分片，每片内 `Name2Id` 把 `real_sender_id` 映射到人
（`db.py:1635-1646, 1767-1792`）。增量读依赖每会话的 `sort_seq` 水位：

- `Chat.GetNewMessage()` 第一次调用**只建水位、固定返回 `[]`**；之后每次批量取 200 条，水位只推进到"实际取回的最后一条"（`wx.py:626-656`）。
- 判"这条是不是我自己发的"用 `sender_username == self_wxid`（`self_wxid` 来自 contact.db，`db.py:564-579`）。**不要**用 `real_sender_id == 2` 判自己：那个 id 是分片内 rowid，含义会变（`wx.py:273-274`；库回读路径用的 `sender_id==2` 是另一套语义，见 §3.4）。

### 2.3 `AddListenAll` 的真相

`WeChat.AddListenAll(cb, discover=True)`（`wx.py:1530-1592` → `db.py:3030-3046`）

**不是** UIA 事件订阅，也**不是**微信侧推送，而是库内部：

- 一个轮询线程，间隔 `LISTEN_INTERVAL = 1s`（`param.py:26-27`）；
- 每个会话一个串行工作线程，回调慢不会阻塞轮询（`db.py:3069-3097, 3121-3129`）；
- 回调签名 `(Message, Chat)`，消息带 `sender_wxid / sender / attr`（`db.py:2138-2162`）；
- 回调确认前水位**不推进**（`_inflight`，`db.py:3108-3119`），水位可 json 持久化。

本项目把它当"带缓冲的推信兜底"用（`wechat2api/wechat_client.py:231-260`）：

```python
# _start_listener()  →  wx.AddListenAll(self._on_all_message); wx.StartListening()
# _on_all_message()  →  按 who(真实 wxid) / nickname(显示名) 过滤目标会话，塞进 self._queue
```

### 2.4 为什么要"推 + 拉"两条路

`wait_for_reply()`（`wechat2api/wechat_client.py:410-446`）每轮同时取两路：

```python
batch = self._drain_queue() + self._poll_db()
```

- `_drain_queue()`（:302）取监听回调塞进队列的消息；
- `_poll_db()`（:311）自己调 `chat.GetNewMessage()` 拉增量——**不依赖库的轮询线程是否还活着**。

两路合并后用 `(sender, text)` 去重（`Incoming.identity()`），避免同一条被推一次又拉一次。
超过 `reply_settle`（默认 3.0s）没有新片段就认为对方说完了，`"\n".join(chunks)` 作为回复返回。
多气泡回复因此会被拼成一段文本。

### 2.5 服务号/公众号：`biz_message_*.db`

上游 `_run_msg_query` 只匹配 `message[\\/]message_\d+\.db`，而"微信支付"这类服务号的**正文**
落在 `message\biz_message_N.db`。症状很典型：会话列表里有摘要，`GetNewMessage()` 永远 0 条。

本项目补了 `wechat2api/db_read.py`：

| 函数 | 作用 |
| --- | --- |
| `biz_shards(db, user)` | 找到该 username 所在的分片库（`BIZ_SHARD_RE`） |
| `latest_biz_seq(db, user)` | 取当前 `sort_seq` 水位（建基线用） |
| `read_biz_rows(db, user, limit=50, since_seq=None, self_wxid="")` | 读增量行，附 `is_self` 标记 |

注意 **biz 分片的 `sort_seq` 是毫秒量级**，和普通消息库不是同一套刻度，所以
`WeChatClient._biz_seq` 是**独立水位**（`wechat2api/wechat_client.py:343-391`），
不能和 `Chat.GetNewMessage()` 的水位混用。

### 2.6 基线：别把历史当回复

每次发送前都先 `sync_baseline()`（`wechat2api/wechat_client.py:393-408`）：

1. 清空队列里已有的残留；
2. 若是本进程首次，调一次 `GetNewMessage()`（**只设水位、返回 `[]`**）；
3. 用 `latest_biz_seq()` 给 biz 分片也建一次水位。

这样"刚打开微信时看到的上一轮聊天记录"不会被当成"我发出去之后对方回的话"。

---

## 3. 发信：从 HTTP 请求到微信里出现一行字

### 3.1 完整调用链

```
WeChatClient.send_text(text)                    wechat2api/wechat_client.py:284
  └─ with self._lock:                           # 本进程串行化
      └─ Chat.SendMsg(text, who=target, exact=…)  wx.py:553  ← @uilock 装饰器 wx.py:552
          └─ self._gui.send_msg(msg, target)      guia.py:2034
              ├─ 快路径（同会话连发）: input_text(fast) → click_send(fast)
              │     click_send 内部 rhythm.gate('send')   guia.py:2024
              ├─ UIA 路径: uia.send_text(text)            uia_driver.py:1264
              │     _paste_into() → rhythm.gate('send')   uia_driver.py:1278
              │     → nap(0.2) → _paste_landed() 回读比对 → SendKeys("{Enter}")
              └─ 坐标/OCR 回退: ensure_visible() + 3 次重试 / 75s 总时限
```

`@uilock`（`utils/lock.py:18-124`）= 进程级 `Lock` + 线程级 `RLock` + 绑定事件循环的
`asyncio.Lock`，线程本地 `depth` 支持同线程重入；**锁粒度是整个被装饰方法**。
也就是说：微信的每一次 GUI 操作在进程内都是串行的——这是"单通道"语义的来源。

### 3.2 输入是剪贴板粘贴，不是逐字敲键

`input_text()`（`guia.py:1875-1932`）实际做法：

1. `get_input_box()` 探测输入框矩形（最多 6 次）；
2. 点输入框取焦点（落点用 `rhythm.point()`，`off` 档回正中）；
3. `Ctrl+A` + `Delete` 清空既有内容；
4. **`Ctrl+V` 整串粘贴**（`guia.py:1893-1896`；UIA 路径同思路，`uia_driver.py:1622-1636`）；
5. 确认输入框里有字才返回 True。

所以"逐字间隔"（`rhythm.type_gap`，`guia.py:380/389`）**只在最后手段的拼音回退里用到**
（`guia.py:1929`）——中文正常路径整串一次粘贴，这一点直接决定了 §5 的实测结论。
中文和表情都走剪贴板，表情码在回读比对时折算成 `U+FFFC`（`uia_driver.py:1660-1668`）。

按键用 `keybd_event` 而不是 `SendInput`：后者在部分会话里会丢事件（`guia.py:357-362`）。

### 3.3 回车前回读比对，回车后一律不重发

这是整个发送链最重要的一条安全设计：

- **回车前**：`_paste_landed()` 读回输入框内容与待发文本比对，相似度低于阈值
  `WxParam.SEND_CONTENT_RATIO`（0.6）就**不清空也不回车**，直接返回失败
  （`uia_driver.py:1280-1289`、`guia.py:1980-2007`）。
  没有这一步，`Ctrl+V` 落空时按回车会发出空消息或上一轮残留内容。
- **回车投递后**：置 `_send_attempted = True`（`uia_driver.py:1292`、`guia.py:2025`）。
  一旦这个标志为真，调用方**只能报失败，不允许重发**——因为"回车已按下但没确认"和
  "回车没按下"在 GUI 层无法区分，重发就会真的发出两条
  （`guia.py:2066-2068`、`guia.py:2085-2087`、`guia.py:2127`）。
- 重试只发生在**提交之前**：整体 3 次 / 75s 总时限（`guia.py:2102-2143`），
  输入阶段内部再 6 次；拼音回退是最后手段。

### 3.4 发送确认（本项目用的是弱确认）

`send_msg(verify=False)`（默认）只确认**输入框已清空**，不查库。
`verify=True` 才会：发送前拍 `sort_seq` 水位 → 发送后 1s×8s 轮询 → 要求
`sender_id == 2` 且正文逐字相等（`guia.py:2214-2293`、`param.py:36-37`）。

本项目调用 `Chat.SendMsg(text, who=target, exact=…)` **没有传 `verify`**，即弱确认。
"消息是否真的到达"由下一轮收信/`/v1/admin/status` 的 `last_message_preview` 反映。

### 3.5 窗口与焦点

- `ensure_visible()`（`guia.py:972-983, 1122-1150`）：把遮挡微信的窗口最小化并置顶微信；
  抢前台时临时清掉 `SPI_SETFOREGROUNDLOCKTIMEOUT` 再恢复（15s 内复用结果）。
- 窗口不可见/最小化 → 直接返回失败"微信窗口不可见（可能锁屏/会话断开）"（`guia.py:2099-2100`）。
- 副作用必须知道：**每次发送都会抢走前台焦点**，屏幕上会看到微信窗口动起来。

### 3.6 UIA 热激活：写 1 个字节换一棵控件树

Qt 的 `QAccessible` 只在某个内部标志字节 == 1 时才响应 `WM_GETOBJECT`，否则只暴露
一个空壳（`uia_driver.py:7-12`，`accessibility.py:501`）。所以：

1. **只写运行中进程内存的 1 个字节**，不落盘修改 `Weixin.dll`（`accessibility.py:560-583`）；
2. 4.1.15.56 **不在**内置 RVA 表里，改为"正则扫 dll + 读 PE 节表 + RIP 回引"推导候选地址，
   最多试 4 个（`accessibility.py:76-87, 543`）；
3. 成功判据是**读回物化**——出现 `mmui::` 控件就算成功，写成功不算（`accessibility.py:503-524`）；
4. 失败不致命：本轮 UIA 不可用，`ensure_window()` 返回 False，调用方退回 OCR/坐标路径
   （`uia_driver.py:314-331, 621-659`）。

本项目在 `_activate_uia()`（`wechat2api/wechat_client.py:183-203`）里主动触发一次，
并把结果（含失败原因）记进 `_hook_info`，`/health` 与 `/v1/admin/status` 都能看到：

```
UIA 热激活结果: {'materialized': True, 'error': '', 'hwnd': 1234567}
```

进程一旦退出，这个字节补丁自然消失——所以它不残留、也不能被"卸载"，下次启动重新激活。

### 3.7 目标会话对象

服务号（如"微信支付"）不在联系人表里，直接按显示名构造会出现"读库读不到"
（`wechat2api/wechat_client.py:205-229`）：

1. 先在 `GetSession()` 的会话列表里按 `name == target` 反查真实 `username`；
2. 查不到再试 `db.username_by_nickname()`；
3. 拿到后写回 `chat._wxid`，让 `GetNewMessage/GetAllMessage` 用真实 username 读库。

---

## 4. 端到端时序（实测）

| 场景 | 实测 | 说明 |
| --- | --- | --- |
| 全新会话（`/clear` + `prompt.txt`） | **24.65s**（区间 24~28s） | 含首次连接、库解密、UIA 激活；初始化本身约 16s |
| 正常一轮（有新内容） | **8.3~8.7s** | 发送 ~1.4s + 等回复 + `reply_settle` 3.0s |
| 无新内容（`reused_reply`） | **0.007~0.01s** | 零微信流量，直接回缓存的上一条回复 |

会话初始化（`initialize_session()`，`wechat2api/wechat_client.py:461-504`）：

```
发 /clear      → 等回复（init_command_timeout 60s）
发 prompt.txt  → 等回复（init_prompt_timeout 120s）
两者都拿到回复 ⇒ initialized=True
```

微信支付（LLM 服务号）对 `/clear` 会回"已开始新对话，之前的聊天内容不再作为参考。"，
对占位 prompt 回"好的呀，您说～"——所以 `init_require_reply: true` 这套判据对它成立。
`init_require_reply: false` 时只在超时打警告。

**初始化很贵**（重置服务号上下文 + 20 多秒），所以：
只有"解析出的会话 id ≠ 已记录 active 会话"且 `reinit_on_switch: true` 才重跑；
已经初始化且已 active 的会话**不会**重发 `/clear`。
另有 Redis 初始化锁（`ttl = max(60, init_command_timeout + init_prompt_timeout + 30)`）防止并发双初始化。

---

## 5. 拟人节流（`wechatauto.rhythm`）与本项目的档位开关

### 5.1 `gate()` 是最小间隔，不是固定延时

`rhythm`（`rhythm.py`）的设计是**两次"对外可见的写动作"之间至少隔一段时间**：

- `gap`：距上次写动作**不足**该间隔才补等（够了就不等），`natural` 档 `uniform(2.5, 6.0)`；
- `burst`/`window`：120s 内写超过 6 次触发冷却 `cooloff` 30~75s；
- `point()`：点击落点四边内缩 15% 随机取点（`off` 档回正中）；
- `move_to()`/`nap()`：光标走曲线 + 步间小睡；
- `type_gap()`/`key_hold()`：仅在拼音回退逐字输入时生效（§3.2）。

状态落盘 `~/.wechatauto/rhythm.json`，**跨进程生效**（`rhythm.py:18-21`）。
`off` 档下 `gate()` 直接短路返回 `0.0`。

调用点：`guia.py:2024`（`click_send`，快路径）、`uia_driver.py:1278`（UIA 发送）、
`guia.py:2384`（发文件）、`sender.py:219`，以及 `forward/labels/moment/group_friend` 等批量动作。

### 5.2 实测：关掉它到底省了多少

同一台机器、同一段 15 字文本（2026-10-27 21:01）：

| 档位 | 连接 `ensure()` | 发送 `send_text()` |
| --- | --- | --- |
| `natural`（库默认） | 2.50s | **1.59s** |
| `off`（本项目默认） | 1.85s | **1.38s** |

差值只有 **0.21s（约 13%）**，原因很明确：

1. `gate()` 只在"距上次写动作不足 gap"时才补等。两次测试之间已远超 6s 上限，
   所以 `gate()` 等待为 **0**；`~/.wechatauto/rhythm.json` 里只有一条
   `{"last": 1793106082.18, "stamps": [1793106082.18], "profile": "natural"}`，可佐证这次写动作没有被延时。
2. 中文发送是整串剪贴板粘贴（§3.2），逐字间隔本来就不参与。
3. 剩下的 0.21s 是点击前的 `nap(0.15~0.4s)` 与 `move_to` 光标曲线。

**结论（修正一个容易误判的直觉）**：节流对"每轮一发的 API 场景"影响很小，
真正会被它咬到的是**短时间连续多次写**——`/clear` 与 `prompt.txt` 相隔十几秒不受影响，
但 120s 内超过 6 次写就会吃到 30~75s 冷却。

### 5.3 本项目怎么开关

配置项 `wechat.rhythm`（`wechat2api/config.py` 的 `WeChatConfig.rhythm`，默认 `"off"`）：

| 取值 | 行为 |
| --- | --- |
| `"off"`（默认） | `set_profile("off")` + `configure(gap=(0,0), burst=0)`，并作废 `WECHATAUTO_WRITE_GAP` / `WECHATAUTO_WRITE_BURST` |
| `"fast"` / `"natural"` / `"calm"` | 直接套该档位 |
| `""`（空串） | 不干预，沿用库里自己的默认档（`natural`） |
| 其它 | 打警告后按 `off` 处理 |

实现是 `_apply_rhythm()`（`wechat2api/wechat_client.py:143-181`），在 `ensure()` 里
**构造 `WeChat()` 之前**调用（`wechat2api/wechat_client.py:124`），所以第一次点击就已经生效。
日志会打出实际档位：

```
拟人节流档位: off（写间隔 0.0~0.0s，120s 上限 0 次，字间 0.00~0.00s；已忽略环境变量 WECHATAUTO_WRITE_GAP）
```

为什么要显式作废那两个环境变量：它们是"切档之后依然生效"的单项覆盖
（`rhythm._env_over` + `_apply_over`），只 `set_profile("off")` 的话
`WECHATAUTO_WRITE_GAP` 会把写间隔又加回来——`tools/selftest.py::test_rhythm()` 专门验了这条。

### 5.4 风险提示（这是拿确定性换风险）

`natural` 档是库作者在账号被微信风控、要求重新登录之后加的（2026-09-21）。
把它默认关掉意味着：**写动作不再有节流，连续/批量操作被风控的概率上升**。
需要恢复时把 `wechat.rhythm` 改成 `natural`（或更慢的 `calm`）即可；
留空串则把决定权交回库里。

---

## 6. 可靠性设计

| 机制 | 位置 | 解决的问题 |
| --- | --- | --- |
| 进程内串行锁 | `_lock`（`wechat_client.py:288`）+ `@uilock`（`wx.py:552`） | 同一个微信窗口只能有一个操作者 |
| 发送前建水位 | `sync_baseline()`（:393） | 历史消息被误当成新回复 |
| 回调 + 轮询双路 | `_drain_queue()` + `_poll_db()`（:427） | 单路漏消息 |
| `(sender, text)` 去重 | `Incoming.identity()`（:434-438） | 同一片段被推一次又拉一次 |
| `_send_attempted` | `guia.py:2025` / `uia_driver.py:1292` | 回车已投递后重发导致重复消息 |
| Redis 初始化锁 | `store.py` `:initlock:{sid}` | 并发请求触发两次 `/clear` |
| 全量历史 diff | `dedupe.py`（`new_session`/`prefix`/`anchor`/`rewritten`/`no_new_content`） | codex 类客户端每次重发全部历史，只转发最新增量 |
| 失败可见 | `WeChatHookError` + `InitReport.steps` | 不吞错，HTTP 层能报出卡在哪一步 |

---

## 7. 已知限制与失效模式

**版本敏感常量**（微信升级后最可能出问题的地方）：

- UIA gate 的 RVA 表里没有 4.1.15.56，靠扫描推导（`accessibility.py:76-87`）——升级后若扫不到，整片没有 UIA 树，会自动降级 OCR/坐标（更慢、更易点错）。
- 密钥提取的内存偏移/掩码：`CFG_PTR_BACK=0x138`、`CFG_OFFSET=0x68`、`CFG_CIPHER_OFF=0x2B8`、XOR 掩码、主密钥 pattern、`KDF_ITER`（`db.py:47-67, 584`）——升级后若失效，连库都读不出来。
- 排序字段 `sort_seq` 的语义（`db.py:1737-1738`）。

**可见的漏消息成因**：新增分片需要重扫（`db.py:1609-1615`）、WAL 合并失败退主库快照可能缺最近消息（`db.py:1459-1475`）、积压超过 `max_backlog` 被截断（`wx.py:630-631`）。

**环境性失败**：锁屏 / 会话断开 → 明确失败而非静默（`guia.py:31, 2099-2100`）；发送抢前台焦点；DPI 变化由布局比例与漂移重校准处理（`guia.py:64-74, 149, 1774-1785`）。

**本项目特有**：`_current_chat` / `_last_input_box` 是进程内状态，因此一个进程只有一套 GUI 上下文；
`wechat2api` 自身不含任何节流代码（只有 `pipeline.py:247` 的 `time.sleep(0.5)` 与
`wechat_client.py` 的 `time.sleep(poll)`），所有人为延时都来自 `wechatauto` 那一层。

---

## 8. 一句话总结

`wechat2api` 做的事可以概括成：

> 用**只读方式**（内存取密钥 + 页级解密 + 临时明文库）把微信的消息库变成可查询的数据源，
> 用**真实输入**（剪贴板粘贴 + 回车，配回读比对与"投递后不重发"）把消息发出去，
> 在两者之间加一层**水位/基线/去重**，让 OpenAI 兼容协议在"没有会话概念"的微信上成立。
