# wechat2api

**请不要公开接口。** **当你让AI查询最近一条订单时可能会泄露隐私！！！**

把**本机真实微信**（Windows 微信 4.x）接成一个 **OpenAI 兼容的 HTTP 服务**。

外部工具（codex / 任意 OpenAI SDK 客户端）按标准格式发请求，请求内容经
「会话判定 → 去重 → 初始化」后驱动真实微信窗口把消息发出去，再把对方回复按
OpenAI 响应格式返回。**不注入 DLL、不伪造登录、不调用任何第三方协议**——它操作的是
你桌面上那个真的微信。

```
外部客户端 ──HTTP(OpenAI 格式)──> wechat2api ──┬── Redis（会话 / 历史 / 锁）
                                              └── 实机微信 4.x
                                                   ├─ 发：UIA 输入框（Weixin.dll Qt 无障碍热补丁）
                                                   └─ 读：本地 SQLCipher 解密库（message_N.db + biz_message_N.db）
```

适合的场景：把只有微信入口的对话（服务号 / 公众号 / 某个好友）接进你的自动化流程或
命令行工具。**不适合**拿来做群发、营销或任何批量骚扰行为。

---

## ⚠️ 使用前必读

1. **需要 3~4 轮对话引导才能进入目标状态。**
   微信官方侧似乎更新过行为（服务号/公众号那一端），实测**不再是"发一次 prompt.txt 就听话"**：
   初始化完成后，通常还要再聊 **3~4 轮**做引导，目标对象才会稳定按你的要求回答。
2. **它会抢微信窗口的焦点。** 发送靠模拟剪贴板粘贴 + 回车，所以运行期间鼠标键盘可能被抢、
   屏幕不能锁、微信窗口不能被最小化，也**不能同时手动用微信**。
3. **有被风控的风险。** 微信风控看的是**动作的时间分布**（固定间隔、永远命中控件正中、
   匀速键入），底层库作者的本机账号就触发过一次风控、随后被要求重新登录，之后才加上
   拟人节流。本项目默认 `wechat.rhythm: "off"`（不等，图确定性、省那 ≈0.2s），
   连续/批量发送更容易触发风控；想稳一点就改成 `natural` 或 `calm`。
4. **会读你本机的微信数据库。** 程序在和你同权限下扫描微信进程内存取密钥、解密本地消息库，
   只读、不改库，但请在**你自己的机器、你自己的账号**上使用。
5. **不要提交隐私文件。** `config.yaml`（含 api_key）、`prompt.txt`、`wechatauto_logs/`
   已在 `.gitignore` 里；拷贝本仓库前请确认这几样没被 `git add -f` 强加进去。

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 x64 |
| Python | **64 位** 3.10+（必须与微信进程位数一致，否则读不到内存） |
| 微信 | **Windows 微信 4.x**（实测 4.1.15.56 可直接用；其他 4.x 小版本首次运行会自己扫符号，见 §8） |
| 权限 | 与微信**同权限**运行（普通用户即可，不需要管理员） |
| Redis | 可选。不装会退回进程内存、重启丢会话状态 |
| 底层库 | 需要 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 提供的 `wechatauto` 包（本项目不含 hook 层） |

微信必须**已登录**、窗口**未最小化**、屏幕**未锁**。

## 2. 安装

```powershell
git clone <本仓库地址> wechat2api
cd wechat2api

# 1) 依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 底层 hook 层（单独一个项目）
#    直接从仓库装：
.venv\Scripts\python.exe -m pip install "wechatauto-replica @ git+https://github.com/fanyuantaier/wechatauto-replica.git"
#    或者已经 clone 到本地了，就按 editable 装：
# .venv\Scripts\python.exe -m pip install -e <wechatauto-replica 的本地路径>

# 3) Redis（可选）
docker run -d --name wechat2api-redis -p 6379:6379 redis:7-alpine
```

## 3. 配置

```powershell
copy config.example.yaml config.yaml     # 配置文件（已在 .gitignore 里）
copy prompt.example.txt  prompt.txt      # 会话初始化提示词（已在 .gitignore 里）
```

所有字段都能用环境变量覆盖，规则 `WECHAT2API__<段名>__<字段名>`，
例如 `$env:WECHAT2API__SERVER__PORT=9000`、`$env:WECHAT2API__WECHAT__TARGET='文件传输助手'`，
环境变量优先级高于 yaml。

| 段 | 字段 | 说明 |
| --- | --- | --- |
| `server` | `host` / `port` | 监听地址，默认 `127.0.0.1:8800` |
| | `api_key` | 非空则除 `/health` 外都要 `Authorization: Bearer <key>`。**服务等于你的微信，别裸奔** |
| | `model_name` | 对外模型名，默认 `wechat-2api` |
| `redis` | `url` / `prefix` / `ttl_seconds` | 会话状态存储；`ttl_seconds=0` 表示不过期 |
| | `require` | `true` 时 Redis 连不上直接启动失败 |
| `wechat` | `target` | 目标会话显示名：服务号名 / 好友昵称 / 群名 |
| | `init_command` | 新会话初始化第一步，默认 `/clear`（清上下文，不是所有目标都支持） |
| | `prompt_file` | 新会话初始化第二步的内容，默认 `prompt.txt` |
| | `reply_timeout` / `reply_settle` | 等回复总超时 / 收到最后一条后静默多久算说完 |
| | `init_command_timeout` / `init_prompt_timeout` | 初始化两步各自的超时 |
| | `reinit_on_switch` | 会话 id 变化时是否重新初始化 |
| | `init_require_reply` | 初始化没等到回复是否算失败（默认 `true`） |
| | `exact` | 目标名是否精确匹配（**名字短就别开模糊匹配，容易发错会话**） |
| | `rhythm` | 拟人节流档位：`off`（默认，不等）/ `fast` / `natural` / `calm`；空串=不干预底层库默认档 |
| | `poll_interval` / `send_timeout` | 收信轮询间隔 / 单次发送超时 |

### 3.1 `prompt.txt` 写什么

这个文件的**全部内容**会在新会话初始化时原样作为一条微信消息发出去（不是系统提示词），
所以用直白的话写要求，例如：

```
你是一个通过微信和我聊天的助手。请用中文、口语化回答，一次一到三句话，
不要用 Markdown、列表或代码块，也不要复述我的问题。
接下来无论我说什么，都请一直遵守上面的要求。
```

改完不用重启：`POST /v1/admin/prompt/reload` 热加载。
**记住 §⚠️ 第 1 条**：一轮 prompt 往往不够，做好多聊几轮引导的准备。

## 4. 启动与自检

```powershell
.venv\Scripts\python.exe -m wechat2api serve     # 起服务（前台，日志直接打在控制台）
.venv\Scripts\python.exe -m wechat2api status    # 看状态：连没连上微信、存储、目标会话
.venv\Scripts\python.exe -m wechat2api probe     # 只探连通性，不操作窗口
```

首次启动会在日志里看到几行关键信息，正常长这样：

```
UIA 热激活结果: {'materialized': True, 'error': '', 'hwnd': ...}
已连接: 本机微信=<你的 wxid> 目标会话=<目标名>(<目标 wxid>)
```

`materialized: True` 表示无障碍通道打开成功（发送走这条路，最稳）。失败会降级到
OCR/坐标模式，仍能用但不稳，参见 §8。

## 5. 怎么调用

### 5.1 curl

```powershell
curl http://127.0.0.1:8800/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <你的 api_key>" `
  -d '{
    "model": "wechat-2api",
    "messages": [
      {"role": "system", "content": "用中文简短回答。"},
      {"role": "user", "content": "你好"}
    ]
  }'
```

### 5.2 OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8800/v1", api_key="<你的 api_key>")
r = client.chat.completions.create(
    model="wechat-2api",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

### 5.3 响应长什么样

标准 OpenAI 结构，外加一个 `wechat2api` 块和几个响应头，方便排障：

```json
{
  "choices": [{"message": {"role": "assistant", "content": "你好呀～"}, "finish_reason": "stop"}],
  "wechat2api": {
    "session_id": "compat_cs_|model=wechat-2api|system=...|first_user=你好",
    "session_hash": "9f73654a47c6",
    "session_source": "content-seed",
    "is_new_session": true,
    "reinitialized": true,
    "reused_reply": false,
    "dedupe": {"mode": "new_session", "common_prefix": 0, "new_count": 1, "new_text": "你好"},
    "forwarded": "你好",
    "elapsed": 27.7
  }
}
```

- `forwarded`：这次**真正发到微信**的内容；`reused_reply: true` 表示这次没有新内容、
  直接复用了缓存回复（**没有打扰微信**）。
- 响应头：`X-Session-Id`（非 ASCII 会百分号编码）、`X-Session-Hash`、`X-Dedupe-Mode`、`X-Session-Source`。
- `stream=true` 走 SSE，但**没有提前量**：微信回复本身是整段到达的，所以等回复拿全后才开流，
  一共三帧——第一帧 `delta` 直接带完整正文 + `role`，第二帧 `finish_reason: "stop"` + `usage`，
  第三帧 `data: [DONE]`。只是为了兼容客户端的流式协议，不做假分片。

### 5.4 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（唯一免鉴权） |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | 主接口，`stream=true` 走 SSE |
| POST | `/v1/completions` | 老式补全接口 |
| GET | `/v1/admin/status` | 运行状态 + 会话总览 |
| GET | `/v1/admin/sessions` | 存储里的会话列表 |
| POST | `/v1/admin/sessions/{sid}/reinit` | 手动重跑 `/clear` + `prompt.txt` |
| DELETE | `/v1/admin/sessions/{sid}` | 清掉某个会话状态 |
| POST | `/v1/admin/prompt/reload` | 改完 `prompt.txt` 热加载 |

## 6. 接入第三方客户端

微信只有一个账号，没法用"谁在调我"区分会话，所以会话 id 从请求里推（判定链见 §7.1）。
对接 codex / Claude Code / opencode / CodeBuddy 这类客户端时：

- 客户端**自带**的 `prompt_cache_key`、`X-Session-Affinity`、`X-Claude-Code-Session-Id`
  等头会被自动识别，通常不用额外配置；
- 想自己控制会话，直接带 `session-id: <任意稳定字符串>`（或 `session_id` / `conversation_id`）即可；
- 会话 id 变了会触发**重新初始化**（`/clear` + `prompt.txt`，约 16~25s），所以别每轮都换 id；
- 同一个会话内，客户端把全量历史重发也没关系——去重逻辑只转发新增部分（§7.3）。

## 7. 它是怎么做到的

一句话：**收**靠解密本地 SQLCipher 消息库 + 水位增量拉取，**发**靠模拟剪贴板粘贴 + 回车，
**心跳**是 1s 轮询。完整技术实现（含 `文件:行号` 证据）见
**[docs/wechat-transport.md](docs/wechat-transport.md)**，这里只讲要点。

### 7.1 会话 id 怎么判

微信只有一个账号，所以只能从请求本身推，逐级降级（与 sub2api 口径一致）：

1. 显式头 `session-id` / `session_id` / `conversation_id`
2. `X-Session-Affinity` / `X-Session-Id` / `X-OpenCode-Session`
3. `X-Conversation-ID`（CodeBuddy）
4. `X-Grok-Conv-Id`（Grok）+ `X-Claude-Code-Session-Id`（Claude Code）
5. body 里的 `prompt_cache_key`（codex 会带）
6. **内容种子**兜底：`compat_cs_|model=..|system=..|first_user=..`
   （只取整轮稳定的字段：model、tools、functions、instructions、开头 system/developer、第一条 user）

拿到种子后算 `session_hash`（xxhash64 取 16 位 hex，缺库时退 sha256）。
超过 255 字符的 id 会被判非法；内容种子超过 200 字符会压成 `compat_cs_<16位hex>`
（**压缩前先求摘要**，否则超长种子会被直接拒掉）。压缩后的 hex 同时写进响应头和
存储键，避免长中文进 HTTP 头（Starlette 响应头是 latin-1，非 ASCII 会 500）。

### 7.2 新会话初始化（会话 id 变了才跑）

存储里存一个 `active`（当前会话 id），解析出的 id ≠ 存储值 = 新会话：

1. 发 `init_command`（默认 `/clear`），等回复；
2. 发 `prompt.txt` 全部内容，等回复；
3. 两步都过 → 该会话标记 `initialized`，才继续转发用户内容；最后更新 `active`。

- 每个会话一把 Redis 锁（`{prefix}:initlock:<sid>`），并发请求不会重复初始化；
- `init_require_reply: true`（默认）时两步都必须等到回复，等不到就失败——不会把
  "没初始化成功"当成成功往下发；目标不保证有回应就改 `false`，此时超时仅告警；
- 顺序不能反：`/clear` → `prompt.txt` → 用户最新内容；
- 代价约 **16~25s**，所以别让会话 id 频繁变化。

### 7.3 去重（codex 式"每次带全量历史"）

codex 这类客户端每个请求都会把整段历史重发一遍，直接转发会把老内容重复发给对方。
所以按会话存一份归一化历史（`{prefix}:hist:<sid>`），每次拿新历史和它比：

1. 逐条比最长公共前缀 → 前缀内条数 = 已见过部分；
2. 前缀对不上时用**锚点兜底**：定位存储历史的最后一条消息在新历史中的位置，转发其后的全部内容；
   但只有 `锚点-1 > 前缀` 且重叠区间存在同下标文本不一致（`_has_rewrite`）时才信锚点，
   避免把"改写过的旧内容"当新内容漏发；
3. 新会话（没历史）→ 原样存，全部转发；
4. 否则剥掉重复前缀，只保留 `user`/`tool`/`function` 尾部，丢掉 `assistant` 和开头的 `system`；
5. 最终取**最后一条 user 消息及其之后**作为真正要发的内容。

去重模式（`X-Dedupe-Mode`）：`new_session` / `prefix` / `anchor` / `rewritten` / `no_new`。
`no_new` = 这次没有新内容，直接复用缓存回复，不再打扰微信。

### 7.4 收发通道

- **发**：整串写进剪贴板 → `Ctrl+A`、`Delete`、`Ctrl+V` 粘进输入框 → 回读比对
  （相似度阈值 0.6，不达标就**既不回车也不清空**）→ 回车提交。回车一投递就置位
  `_send_attempted`，**绝不重发**，避免重复消息。
- **收**：底层库不是"微信推送"，而是**1s 轮询**本地消息库；本项目另加一路自己拉 biz 分片，
  两路合并去重。水位用 `sort_seq`，只在回调确认后才推进。
- **UIA**：往运行中的 `Weixin.dll` 内存写**一个字节**打开 Qt 无障碍门（不注入 DLL，
  进程退出即失效），读回出现 `mmui::` 控件才算成功；失败自动降级 OCR/坐标。

### 7.5 公众号 / 服务号消息在另一个库里

「微信支付」这类服务号的正文落在 `message\biz_message_N.db`，而底层库只扫
`message\message_N.db`——会话列表里明明有摘要，`GetAllMessage()` 却永远 0 条。
`wechat2api/db_read.py` 补齐这块：按 `Msg_<md5(username)>` 定位会话表、
按**本分片自己的 `Name2Id`** 解析发送者（不能写死"2=自己"）、
正文解码复用底层实现、增量用自维护的 `sort_seq` 水位。

## 8. FAQ / 排障

**Q：第一轮它答非所问，或者完全不按 prompt.txt 来？**
这不一定是 bug。见 §⚠️ 第 1 条：官方侧更新后通常需要 **3~4 轮引导**才进入目标状态。
先把引导语完善进 `prompt.txt`，再多聊几轮；仍不行就 `POST /v1/admin/sessions/{sid}/reinit`
重跑初始化。

**Q：日志报 `UIA 热激活` 失败 / `materialized: False`？**
说明没打开无障碍通道，会降级到 OCR + 坐标模式（能用但更慢更飘）。检查：微信版本是否
4.x、窗口是否可见未最小化、是否与微信**同权限**运行。新版本首次运行会自己扫 dll 找符号
（最多 4 次尝试），失败就说明这个版本的结构变了，需要更新底层库的符号推导。

**Q：`read` 一直是 0 条？**
如果目标是服务号/公众号，正常库路径读不到（§7.5）。也确认目标会话名没配错、
`exact` 与名字长短是否匹配。

**Q：报连不上 Redis / 掉会话状态？**
`redis.require: false` 时会退回进程内存并告警，功能可用但重启即丢（会重新初始化）。

**Q：发送成功但对方没收到？**
发送确认有强弱之分：默认只检查"输入框已清空"（弱确认）。若回报成功而实际没发出去，
看日志里回车前**回读比对**那一段（阈值 0.6），内容没粘进输入框时不会回车。

**Q：能锁屏 / 后台跑吗？**
不能。发送要抢焦点，屏幕锁了、窗口最小化都会失败。

**Q：能多开会话 / 多账号吗？**
当前实现是**一个进程对一个微信账号 + 一个目标会话**。多目标需要各自起进程（配不同
Redis `prefix` 和端口），多账号本机同时登录一个微信也不现实。

## 9. 自测

离线跑，不需要微信、不需要 Redis（会临时隔离 Redis 键，不影响线上会话）：

```powershell
.venv\Scripts\python.exe tools\selftest.py    # 会话 id / 去重 / 存储 / 节流档位，74 项
.venv\Scripts\python.exe tools\e2e_fake.py    # HTTP 全链路（假微信 + 假 Redis），55 项
```

实机探针（会真的发微信消息，谨慎）：

```powershell
.venv\Scripts\python.exe tools\probe_real.py read 15   # 只读最近 15 条
.venv\Scripts\python.exe tools\probe_real.py send 你好  # 真发一条
.venv\Scripts\python.exe tools\probe_real.py wait 60   # 等回复
```

## 10. 目录结构

```
wechat2api/
  config.example.yaml   配置模板（复制成 config.yaml；config.yaml 不入库）
  prompt.example.txt    初始化提示词模板（复制成 prompt.txt；prompt.txt 不入库）
  requirements.txt
  wechat2api/
    api.py              FastAPI 路由：/v1/* + /v1/admin/*
    pipeline.py         一轮请求的编排：会话判定 → 初始化 → 去重 → 发送 → 等回复
    session_id.py       会话 id 判定链与摘要
    dedupe.py           历史比对与增量提取
    store.py            Redis / 内存 会话存储（历史、状态、回复缓存、初始化锁）
    wechat_client.py    实机微信适配：UIA 热激活、目标解析、收发、水位
    db_read.py          服务号 biz_message_*.db 读取
    config.py           配置加载（yaml + 环境变量覆盖）
    main.py             CLI：serve / probe / status / send
  docs/
    wechat-transport.md 与实机微信通信的技术实现（含 file:line 证据）
  tools/
    selftest.py         离线自测
    e2e_fake.py         假微信假 Redis 的 HTTP 全链路测试
    probe_real.py       实机探针
```

## 11. 免责声明

本项目通过 UI 自动化 + 读取本地消息库的方式操作**你自己的**微信账号，属于非官方用法：
可能违反微信用户协议、可能导致账号被限制或封禁，也可能因微信版本升级而失效。
请自行评估风险，仅用于个人学习与自动化，不要用于群发、营销、骚扰或任何违法用途。
作者不对账号风险、数据丢失或由此产生的任何后果负责。
