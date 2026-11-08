# Changelog

All notable changes to this plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), versioning follows [SemVer](https://semver.org/).

## [2.0.1] - 2026-11-09

### Fixed
- 接口请求未带 `User-Agent`，被 Cloudflare 以 `403 error code: 1010` 拦截，
  导致所有审批都退化成「留给管理员人工处理」。现显式发送 `AstrBot-XxtGate/2.0.1`，
  并在测试中断言该请求头存在。

## [2.0.0] - 2026-11-08

### Changed
- 只保留入群审批：从申请理由取出学习通手机号，调用平台 `POST /api/bot/member-check` 确认后同意，否则拒绝并告知原因；接口出错时留给管理员人工审批
- 配置简化为目标群号（每行一个）、平台地址、密钥、超时、欢迎语
- 日志里的手机号统一打码

### Added
- 新人进群 @ 欢迎：「欢迎张同学进群，感谢您的支持。」，姓氏来自平台，支持自定义模板

### Removed
- 理由白名单匹配（exact / contains）
- 进群算术题验证、失败计数、自动拉黑
- 管理员群管命令（拉黑 / 解黑 / 黑名单 / 禁言 / 解禁 / 踢）和 `/joingate`
- 旧测试，改为 `tests/test_gate.py`

## [1.1.0] - 2026-11-09

### Added
- 进群验证：新人进群 @ 出算术题，限时（默认 120s）作答
- 答错 / 超时踢出群
- 失败次数跨群累计并持久化，达阈值（默认 2 次）自动拉黑
- 拉黑时踢人带 `reject_add_request=true`，QQ 层面拒绝再次加群
- 管理员群管命令：`/拉黑` `/解黑` `/黑名单` `/禁言` `/解禁` `/踢` `/群管`
- 命令支持群聊与私聊（私聊需指定群号），支持中文时长 `10m` / `2h` / `1d` / `永久`
- `admin_qq` 配置项，独立于 AstrBot 的 `admins_id`

### Fixed
- 入群理由被「问题/答案」包装时 `exact` 匹配失败：现自动抽取「答案：」后的内容参与匹配
- 管理员命令无响应：AstrBot 唤醒阶段会剥离 `wake_prefix`，导致 `message_str` 丢失开头的 `/`；改为读取原始消息并兼容 `[CQ:at,...]` 前缀与全角 `／`
- 重复处理同一申请时的 `already refuse msg by self` 报错降级为 INFO

### Tests
- `test_decision.py`：17 条理由匹配用例
- `test_verify.py`：失败计数 / 拉黑阈值 / 踢人参数 / 答案归一化 / 超时守卫
- `test_admin.py`：命令文本提取 / 管理员识别 / 时长解析 / 禁言与踢人参数 / 黑名单共用

## [1.0.0] - 2026-11-08

### Added
- 入群申请按理由自动审批（`exact` / `contains`）
- 分群规则、未填理由策略、自定义拒绝理由
- 黑名单用户申请直接拒绝
- `/joingate` 查询与干跑命令
