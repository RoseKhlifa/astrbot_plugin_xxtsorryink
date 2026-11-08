# astrbot_plugin_xxtsorryink

> 官方群入群门禁：**按学习通手机号审批入群** + **进群 @ 欢迎**

AstrBot 插件，面向 `aiocqhttp`（OneBot v11，配合 SnowLuma / NapCat 等协议端）。
只对配置里列出的群生效，其他群**完全不干预**。

---

## 工作流程

```
入群申请（理由 = 学习通手机号）
        │
        ▼
 取出 11 位手机号 ──没有──► 拒绝：请在申请理由里填写 11 位学习通手机号
        │
        ▼
 POST /api/bot/member-check
        │
   ┌────┴─────┬────────────────┐
   ▼          ▼                ▼
 在平台里   没在平台登录过    接口出错/超时/密钥不对
   │          │                │
  同意     拒绝并告知原因    不处理，留给管理员人工审批
   │
   ▼
 进群 → @ 新人：欢迎张同学进群，感谢您的支持。
```

- 只看手机号对不对：同一个手机号对应多个 QQ 也都放行，不做绑定。
- 手机号识别兼容「问题：…\n答案：…」包装、全角数字、空格、横线、`+86`。
- 平台只返回「是 / 否」和姓氏，不返回全名、学校等资料；日志里的手机号一律打码成 `138****0001`。
- 欢迎语里的姓氏来自学习通姓名（支持欧阳、司马等常见复姓）。名字不是汉字开头，或者新人是管理员手动拉进来的，就称呼「新同学」。

---

## 安装

AstrBot WebUI → 插件管理 → 从 URL 安装：

```
https://github.com/RoseKhlifa/astrbot_plugin_xxtsorryink
```

无第三方依赖（调用平台接口用标准库 `urllib`）。

### 前提条件

- 协议端需上报 OneBot v11 的 `group_request`（`sub_type=add`）与 `notice`（`notice_type=group_increase`）事件
- **Bot 必须是群管理员**，否则 `set_group_add_request` 会因权限失败
- 平台服务器的 `.env` 里设置了 `XXT_BOT_TOKEN=<随机长字符串>` 并已重启平台（未设置时接口返回 404）

---

## 配置

WebUI → 插件管理 → 本插件 → 配置。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 入群审批总开关 |
| `group_ids` | text | — | 目标群号，每行一个 |
| `api_base` | string | — | 平台地址，例如 `http://127.0.0.1:18768`（不要带 `/api`）。AstrBot 跑在 Docker 里时 `127.0.0.1` 指的是容器自己，要填宿主机地址 |
| `api_token` | string | — | 与平台 `.env` 里的 `XXT_BOT_TOKEN` 一致 |
| `timeout_seconds` | float | `8` | 接口超时 |
| `welcome_enabled` | bool | `true` | 新人进群是否 @ 欢迎 |
| `welcome_template` | string | `欢迎{surname}同学进群，感谢您的支持。` | `{surname}` 换成姓氏，不知道时换成「新」 |

QQ 群的入群方式设成「需要回答问题并由管理员审核」，问题写「请填写你的学习通手机号」。

### 平台返回与结果

| 情况 | 结果 | 回给申请人 |
|---|---|---|
| 号码在平台里 | 同意 | — |
| 理由里没有手机号 | 拒绝（不调接口） | 请在申请理由里填写 11 位学习通手机号 |
| 号码没在平台登录过 | 拒绝 | 这个手机号还没有在平台登录过，请先登录后再申请 |
| 接口出错 / 超时 / 密钥不对 | 不处理 | —（留在群申请列表里等管理员） |

---

## 测试

用假 bot 和本地假接口跑，不依赖真实平台：

```bash
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_xxtsorryink/tests/test_gate.py
```

覆盖手机号提取与打码、审批判定、真实 HTTP 调用、接口出错不处理、一个号码多个 QQ、欢迎语（含复姓、无姓氏、自定义模板、开关、非目标群、机器人自己进群）。输出 `ALL PASSED` 即通过。

---

## 常见问题

**Q：申请没被处理？**

```bash
docker logs -f astrbot 2>&1 | grep JoinGate
```

- 日志出现 `校验接口出错`：检查 `api_base` 能否从 AstrBot 所在环境访问、`api_token` 是否和平台一致
- **完全没有日志**：群号不在 `group_ids` 里，或协议端没上报 `group_request` 事件

**Q：报 `already refuse msg by self`？**

同一申请被重复处理过，第一次已经生效，插件把这类情况降级为 INFO 日志。

---

## 目录结构

```
astrbot_plugin_xxtsorryink/
├── main.py              # 插件主体（入群审批 + 欢迎）
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # WebUI 配置 schema
├── tests/
│   └── test_gate.py     # 自测
├── CHANGELOG.md
├── LICENSE
└── README.md
```

---

## 许可证

[MIT](LICENSE)
