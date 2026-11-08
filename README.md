# astrbot_plugin_xxtsorryink

> 入群门禁：**按申请理由自动审批**（含**学习通手机号校验**）+ **进群做题验证** + **管理员群管命令**

AstrBot 插件，面向 `aiocqhttp`（OneBot v11，配合 SnowLuma / NapCat 等协议端）。
只对配置里列出的群生效，其他群**完全不干预**。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能](#功能)
- [工作流程](#工作流程)
- [安装](#安装)
- [配置](#配置)
- [管理员命令](#管理员命令)
- [状态与持久化](#状态与持久化)
- [实现说明](#实现说明)
- [测试](#测试)
- [常见问题](#常见问题)
- [目录结构](#目录结构)
- [许可证](#许可证)

---

## 它解决什么问题

群里经常被机器人/广告号乱加，靠人工审批很累。这个插件做两件事：

1. **申请阶段筛人** —— 申请人填的入群理由必须等于（或包含）你设定的口令，否则自动拒绝。
2. **进群阶段验人** —— 进来只是第一步，还得在限时内答对一道算术题，否则踢出去；
   屡教不改的自动拉黑，以后连申请都过不了。

同时附带一组给管理员用的群管命令（拉黑 / 禁言 / 踢人），群里或私聊都能用。

---

## 功能

### 1. 入群申请审批

| 能力 | 说明 |
|---|---|
| 理由白名单 | 每个群可配置多条放行理由 |
| 两种匹配 | `exact` 完全一致 / `contains` 包含关键词 |
| 自动剥离入群问题 | 群开了「入群问题」时，协议端给的 comment 形如 `问题：你好\n答案：1433223`，插件会自动抽出「答案：」后的内容再匹配，`exact` 依然可用 |
| 黑名单优先 | 黑名单用户申请直接拒绝，不看理由 |
| 未填理由策略 | 可配置「拒绝」或「放行」 |
| 手机号校验 | `phone` 模式：理由里填学习通手机号，调用平台接口确认是平台用户才放行，见下文 |

### 2. 进群验证

| 能力 | 说明 |
|---|---|
| @ 出题 | 新人进群后 @ 对方并出一道 `a + b` 算术题 |
| 限时 | 默认 120 秒，可配置 |
| 答对 | 提示「回答正确，欢迎加入」，验证通过 |
| 答错 | **立即踢出** |
| 超时 | 同样记失败并踢出 |
| 累计拉黑 | 失败次数跨群累计、持久化；达阈值（默认 2 次）后踢出并**拒绝再次加群** |
| 抗误判 | 只对「含数字」的消息判题，闲聊/表情不会触发踢人 |

### 3. 管理员群管命令

拉黑 / 解黑 / 黑名单 / 禁言 / 解禁 / 踢人，群聊直接可用，私聊指定群号也可用。
仅配置内的管理员 QQ 有效。

---

## 工作流程

```
                      ┌─────────────────────────────┐
 入群申请 ───────────► │ 理由 == 白名单?              │
                      │  └─ 黑名单? → 直接拒绝       │
                      └──────────┬──────────────────┘
                                 │ 同意
                                 ▼
                      ┌─────────────────────────────┐
 进群通知 ───────────► │ @新人 出题 a+b，限时 T 秒     │
                      └──────────┬──────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
           答对               答错               超时
              │                  │                  │
         「验证通过」      记 1 次失败         记 1 次失败
                               └────────┬─────────┘
                                        ▼
                              ┌──────────────────┐
                              │ 失败次数 ≥ N ?    │
                              └────┬────────┬────┘
                                  是        否
                                   │         │
                              踢出+拉黑    仅踢出
                          (reject_add_request=true)
```

---

## 安装

### 方式一：WebUI 从 URL 安装（推荐）

AstrBot WebUI → 插件管理 → 从 URL 安装：

```
https://github.com/RoseKhlifa/astrbot_plugin_xxtsorryink
```

### 方式二：手动

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/RoseKhlifa/astrbot_plugin_xxtsorryink.git
# 重启 AstrBot 或热重载插件
```

无第三方依赖，纯 AstrBot SDK（调用平台接口用标准库 `urllib`）。

### 前提条件

- 协议端需上报 OneBot v11 的 `group_request`（`sub_type=add`）与
  `notice`（`notice_type=group_increase`）事件
- **Bot 必须是群管理员**，否则 `set_group_add_request` / `set_group_kick` /
  `set_group_ban` 都会因权限失败

---

## 配置

WebUI → 插件管理 → 入群门禁 → 配置。

### 全局

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 入群申请总开关 |
| `admin_qq` | text | — | 管理员 QQ，每行一个 |

### 分群规则 `rules`

`template_list`，可添加多条，每条的字段：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `group_id` | string | — | 目标群号 |
| `allow_reasons` | text | — | 放行理由，**每行一条** |
| `match_mode` | string | `exact` | `exact` 完全匹配 / `contains` 包含 / `phone` 手机号校验 |
| `reject_reason` | string | 申请理由不正确… | 拒绝时回给申请人 |
| `on_empty_reason` | string | `reject` | 未填理由时：拒绝 / 放行 |
| `verify_new_member` | bool | `true` | 本群是否开启进群验证 |

### 平台接口 `xxt`（手机号校验用）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_base` | string | — | 平台地址，例如 `http://127.0.0.1:18768`（不要带 `/api`） |
| `api_token` | string | — | 与平台 `.env` 里的 `XXT_BOT_TOKEN` 一致 |
| `timeout_seconds` | float | `8` | 接口超时 |
| `on_api_error` | string | `ignore` | 接口出错时：`ignore` 不处理留给人工 / `reject` 拒绝 / `approve` 放行 |

### 手机号校验（`phone` 模式）

用来只让平台的真实用户进官方群：

1. 平台服务器的 `.env` 加一行 `XXT_BOT_TOKEN=<随机长字符串>`，重启平台。
2. 插件配置 `xxt.api_base`、`xxt.api_token`，目标群的规则把 `match_mode` 设成 `phone`（`allow_reasons` 可留空）。
3. QQ 群的入群问题设成「请填写你的学习通手机号」。

插件从理由（优先「答案：」后的内容）里取出 11 位手机号，全角数字、空格、横线、`+86` 都能识别，然后调用 `POST /api/bot/member-check`：

| 平台返回 | 结果 | 回给申请人 |
|---|---|---|
| `ok` | 放行，并把这个 QQ 绑定到该账号 | — |
| 没取到手机号 | 拒绝（不调接口） | 请在申请理由里填写 11 位学习通手机号 |
| `not_found` | 拒绝 | 这个手机号还没有在平台登录过，请先登录后再申请 |
| `phone_taken` | 拒绝 | 这个手机号已经被另一个 QQ 用过了，如有疑问请联系管理员 |
| `qq_bound` | 拒绝 | 这个 QQ 已经绑定了别的手机号，请填写之前用过的那个 |
| 接口出错 / 超时 / 密钥不对 | 按 `on_api_error` | 默认不处理，申请留在群里等管理员 |

- 一个账号只能放一个 QQ 进来，号码被转给别人也带不进第二个人。用户换 QQ 时，管理员用 `/解绑 <QQ或手机号>` 解除。
- 平台只回答「是 / 否」，不会返回姓名、学校等资料。
- 日志里的手机号一律打码成 `138****0001`。
- `/joingate test <群号> <理由>` 在 phone 模式下只检查能否取到手机号，不调用接口，也不会绑定。

### 进群验证 `verify`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 验证总开关 |
| `timeout_seconds` | int | `120` | 答题限时（秒） |
| `operand_max` | int | `20` | 题目操作数上限，`a,b ∈ [1, 上限]` |
| `max_failures` | int | `2` | 累计失败多少次拉黑 |
| `blacklist_on_max` | bool | `true` | 达阈值时拉黑 |
| `kick_on_wrong` | bool | `true` | 答错/超时是否踢出 |

### 配置示例

```jsonc
{
  "enabled": true,
  "admin_qq": "2221542777",
  "rules": [
    {
      "__template_key": "rule",
      "group_id": "2163040959",
      "allow_reasons": "1433223",
      "match_mode": "exact",
      "reject_reason": "申请理由不正确，请按群公告填写",
      "on_empty_reason": "reject",
      "verify_new_member": true
    }
  ],
  "verify": {
    "enabled": true,
    "timeout_seconds": 120,
    "operand_max": 20,
    "max_failures": 2,
    "blacklist_on_max": true,
    "kick_on_wrong": true
  }
}
```

配置文件实际位置：

```
AstrBot/data/config/astrbot_plugin_xxtsorryink_config.json
```

---

## 管理员命令

仅 `admin_qq` 里的 QQ 有效。其他人发送会被静默忽略（日志留痕）。

**群聊**：`/<命令> <QQ> [参数]`
**私聊**：`/<命令> <群号> <QQ> [参数]`

| 命令 | 说明 | 示例 |
|---|---|---|
| `/群管` `/管理` `/帮助` | 查看全部命令 | `/群管` |
| `/拉黑 <QQ>` | 加入黑名单；在群里执行会同时踢出并拒绝再加群 | `/拉黑 123456` |
| `/解黑 <QQ>` | 解除黑名单 | `/解黑 123456` |
| `/黑名单` | 查看当前黑名单 | `/黑名单` |
| `/禁言 <QQ> [时长]` | 默认 10 分钟 | `/禁言 123456 2h` |
| `/解禁 <QQ>` | 解除禁言 | `/解禁 123456` |
| `/踢 <QQ> [黑]` | 踢出；带 `黑` 同时拒绝再加群 | `/踢 123456 黑` |
| `/解绑 <QQ或手机号>` | 解除手机号与 QQ 的绑定（用户换 QQ 时用），不需要群号 | `/解绑 123456` |

**时长写法**：`600` / `600s` / `10m` / `2h` / `1d` / `30秒` / `永久`
（自动裁剪到 QQ 上限 30 天）

### 内置查询命令

`/joingate`（需 AstrBot 管理员）：

| 参数 | 说明 |
|---|---|
| 无 / `status` | 查看规则与验证配置 |
| `test <群号> <理由>` | **干跑**一次审批判定，不调用 API |
| `fails` | 查看所有失败计数 |
| `banlist` | 查看黑名单 |
| `ban <QQ>` / `unban <QQ>` / `reset <QQ>` | 手动维护 |

`test` 很适合上线前验证匹配逻辑，不用真去申请入群。

---

## 状态与持久化

| 数据 | 存储 | 说明 |
|---|---|---|
| 失败计数 | 插件 KV (`verify_failures`) | 跨群累计，重启不丢 |
| 黑名单 | 插件 KV (`verify_blacklist`) | 与 `/拉黑` **共用同一份**，不会出现两边不一致 |
| 进行中的验证 | 内存 | 重启后丢失（新人需重新申请，可接受） |

---

## 实现说明

几个值得记下来的点，给后续维护/二次开发者：

### 1. 申请理由为什么不能直接 `exact`

群开启「入群问题」后，协议端给的 `comment` 是**整段**文本：

```
问题：你好
答案：1433223
```

直接拿 `1433223` 做 `exact` 永远匹配不上。插件的做法是

```
候选值 = [完整原文, 「答案：」后的内容, 其后的最后一行]
任一候选值 == 配置理由 → 放行
```

保留 `exact` 的严格性（`14332230` 不会被误放行），同时兼容包装。

### 2. `message_str` 里的 `/` 会被 AstrBot 删掉

AstrBot 唤醒阶段会剥离 `wake_prefix`（默认 `/`）：

```python
event.message_str = message_str[len(wake_prefix):].strip()   # "/管理" → "管理"
```

所以**不能**用 `message_str` 判断命令。插件改为回看原始消息
`message_obj.raw_message`（`raw_message` 字段保留斜杠），
并额外处理 `[CQ:at,qq=...] /命令` 前缀与全角斜杠 `／`。

### 3. 答题判定为什么不处理所有消息

只对**含数字**的消息判题。否则新人随手发个「你好」「😂」就被判错踢人。
超时兜底已经能覆盖真正不答题的情况。

### 4. 踢人时的拉黑标记

`set_group_kick` 的 `reject_add_request` 参数：

- 第 1 次失败 → `false`（允许重新申请，符合「累计两次」的设计）
- 达到阈值 → `true`（QQ 层面拒绝再次加群）

配合本地黑名单，形成「QQ 层 + 插件层」双保险。

---

## 测试

测试为纯逻辑自测，使用假 bot + 内存 KV，不依赖真实平台。

```bash
# 在 AstrBot 容器内执行
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_xxtsorryink/tests/test_decision.py
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_xxtsorryink/tests/test_verify.py
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_xxtsorryink/tests/test_admin.py
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_xxtsorryink/tests/test_phone.py
```

| 文件 | 覆盖 |
|---|---|
| `test_decision.py` | 17 条理由匹配（问题包装、严格性、空理由、非目标群） |
| `test_verify.py` | 失败计数、拉黑阈值、踢人参数、答案归一化、超时守卫 |
| `test_admin.py` | 命令文本提取、管理员识别、时长解析、禁言/踢人参数、黑名单共用 |
| `test_phone.py` | 手机号提取与打码、phone 模式判定、本地假接口的真实 HTTP 调用、接口出错策略、解绑 |

全部输出 `ALL PASSED` 即通过。

---

## 常见问题

**Q：申请理由填对了还是被拒？**

看日志：

```bash
docker logs -f astrbot 2>&1 | grep JoinGate
```

会打印真实 `comment` 与判定依据，例如：

```
[JoinGate] 群 2163040959 用户 10001 理由「问题：你好 ⏎ 答案：1433223」-> 同意（理由完全匹配（抽取的答案））
```

如果日志显示 `理由不在放行名单`，把配置里的理由改成日志中「抽取的答案」实际的值。
如果**完全没有日志**，说明协议端没上报 `group_request` 事件。

**Q：报 `already refuse msg by self`？**

同一申请被重复处理过（你重试了多次）。第一次处理已经生效，忽略即可，插件已把这类情况降级为 INFO 日志。

**Q：踢人/禁言失败？**

- Bot 不是群管理员
- 目标 QQ 是群主
- 参数中的群号/QQ 写错

**Q：管理员命令没反应？**

- 发送者 QQ 不在 `admin_qq` 里
- 群聊里必须带 `/`（`/群管`），不带斜杠的「群管」按普通聊天处理
- 查看日志是否有 `非管理员 xxx 尝试执行`

**Q：多次失败但没拉黑？**

确认 `verify.blacklist_on_max` 为 `true`，且失败计数存在 KV 里（`/joingate fails` 可查）。

---

## 目录结构

```
astrbot_plugin_xxtsorryink/
├── main.py              # 插件主体（审批 + 手机号校验 + 验证 + 管理员命令）
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # WebUI 配置 schema
├── tests/
│   ├── test_decision.py # 理由匹配
│   ├── test_verify.py   # 验证流程
│   ├── test_admin.py    # 管理员命令
│   └── test_phone.py    # 手机号校验
├── CHANGELOG.md
├── LICENSE
└── README.md
```

---

## 许可证

[MIT](LICENSE)
