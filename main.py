"""入群审批 + 入群欢迎（OneBot v11 / aiocqhttp）。

1. 入群申请：从申请理由里取出学习通手机号，调用平台接口确认这个号码
   在平台登录过就同意，否则拒绝并告诉申请人原因。接口出错时不处理，
   留给管理员人工审批。
2. 新人进群：@ 对方发欢迎语，用平台给的姓氏称呼（欢迎张同学进群）。
3. 群成员同步：定时（以及有人进群后）把目标群的成员 QQ 号报给平台，
   平台据此判断「加入官方 QQ 群」任务是否完成。审批时也会把申请人的
   QQ 号一起报上去，平台才知道这个 QQ 属于哪个账号。

未配置的群完全不干预。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

LOG_PREFIX = "[JoinGate]"

# Cloudflare 会按 UA 拦截裸 urllib（返回 403 error code: 1010），
# 必须显式带一个正常的 User-Agent，否则所有审批都会退化成人工处理。
_USER_AGENT = "AstrBot-XxtGate/2.1.0"

PHONE_MISSING = "请在申请理由里填写 11 位学习通手机号"
DEFAULT_WELCOME = "欢迎{surname}同学进群，感谢您的支持。"
_CIALLO_REPLY = "Ciallo～(∠・ω< )⌒★"
WELCOME_TTL = 86400  # 审批后一天内进群都能用上姓氏

# 协议端会把「入群问题 + 答案」整体塞进 comment，形如：
#   问题：请填写学习通手机号
#   答案：13800000000
_ANSWER_MARKERS = ("答案：", "答案:", "答案=", "Answer:", "answer:")
_PHONE_RE = re.compile(r"(?<!\d)(?:86)?(1\d{10})(?!\d)")


def _get(raw: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / aiocqhttp Event / 普通对象三种取值方式。"""
    if raw is None:
        return default
    if isinstance(raw, dict):
        return raw.get(key, default)
    getter = getattr(raw, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                return getter(key)
            except Exception:  # noqa: BLE001
                pass
    return getattr(raw, key, default)


def _answer_candidates(comment: str) -> list[str]:
    """返回用于提取的候选文本：完整原文，以及从里面抽出的答案部分。"""
    raw = (comment or "").strip()
    cands: list[str] = [raw] if raw else []
    for mark in _ANSWER_MARKERS:
        idx = raw.rfind(mark)
        if idx < 0:
            continue
        tail = raw[idx + len(mark) :].strip()
        if tail:
            cands.append(tail)
        break
    return cands


def _extract_phone(comment: str) -> str | None:
    """从申请理由里取出 11 位手机号。

    先看「答案：」后的内容，再看完整原文；全角数字、空格、横线、+86 都能认。
    """
    for cand in reversed(_answer_candidates(comment)):
        text = unicodedata.normalize("NFKC", cand)
        text = re.sub(r"[\s\-+]", "", text)
        m = _PHONE_RE.search(text)
        if m:
            return m.group(1)
    return None


def _mask_phone(phone: str) -> str:
    """日志里只留前三后四。"""
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else "***"


def _parse_groups(value: Any) -> set[str]:
    """每行一个群号（也接受逗号分隔或列表）。"""
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[\s,，]+", str(value or ""))
    return {x.strip() for x in items if x.strip().isdigit()}


def _command_body(event: AstrMessageEvent) -> str | None:
    """提取「/命令」后面的大文。

    AstrBot 的唤醒阶段会把 wake_prefix（默认 "/"）从 event.message_str
    里删掉，所以不能只看 message_str，必须回看原始消息才能判断是否带斜杠。
    """
    raw = getattr(event.message_obj, "raw_message", None)
    candidates: list[str] = []

    raw_text = _get(raw, "raw_message")
    if isinstance(raw_text, str) and raw_text:
        candidates.append(raw_text)

    msg = _get(raw, "message")
    if isinstance(msg, str):
        candidates.append(msg)
    elif isinstance(msg, list):
        parts = []
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                data = seg.get("data") or {}
                parts.append(str(data.get("text") or ""))
        if parts:
            candidates.append("".join(parts))

    if getattr(event, "message_str", None):
        candidates.append(event.message_str)

    for cand in candidates:
        c = cand.strip()
        # 去掉 @ 之类的 CQ 码前缀，例如 "[CQ:at,qq=123] /ciallo"
        c = re.sub(r"^(?:\[CQ:[^\]]+\])+", "", c).strip()
        if c.startswith(("/", "／")):
            return c[1:].strip()
    return None


class JoinGate(Star):
    """按学习通手机号审批入群，进群后 @ 欢迎。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        # (群号, QQ) -> (姓氏, 审批时间)，进群时用来称呼
        self._surnames: dict[tuple[str, str], tuple[str, float]] = {}
        self._sync_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        self._ensure_sync_loop()

    async def terminate(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()

    # ------------------------------------------------------------------ 配置
    def _groups(self) -> set[str]:
        return _parse_groups(self.cfg.get("group_ids"))

    def _api_cfg(self) -> dict[str, Any]:
        try:
            timeout = min(30.0, max(2.0, float(self.cfg.get("timeout_seconds", 8))))
        except (TypeError, ValueError):
            timeout = 8.0
        return {
            "api_base": str(self.cfg.get("api_base") or "").strip().rstrip("/"),
            "api_token": str(self.cfg.get("api_token") or "").strip(),
            "timeout": timeout,
        }

    def _sync_minutes(self) -> int:
        try:
            return max(0, int(self.cfg.get("member_sync_minutes", 10)))
        except (TypeError, ValueError):
            return 10

    def _welcome_text(self, surname: str) -> str:
        template = str(self.cfg.get("welcome_template") or DEFAULT_WELCOME)
        # 不知道姓氏时：欢迎新同学进群
        return template.replace("{surname}", surname or "新")

    # ------------------------------------------------------------ 平台接口
    def _post_sync(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """同步调用平台接口（放在线程里跑）。失败抛 RuntimeError。"""
        api = self._api_cfg()
        if not api["api_base"] or not api["api_token"]:
            raise RuntimeError("未配置平台地址或密钥")
        req = urllib.request.Request(
            api["api_base"] + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api['api_token']}",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=api["timeout"]) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()[:120]
            raise RuntimeError(f"HTTP {exc.code} {body}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    async def _member_check(self, phone: str, qq: str = "", group_id: str = "") -> dict[str, Any]:
        # qq 让平台记住这个 QQ 属于哪个账号（「加入官方 QQ 群」任务要用）
        body = {"phone": phone, "qq": qq, "group_id": group_id}
        return await asyncio.to_thread(self._post_sync, "/api/bot/member-check", body)

    async def _decide(
        self, comment: str, user_id: str = "", group_id: str = ""
    ) -> tuple[bool | None, str, str, str]:
        """返回 (放行? None=留给人工, 回给申请人的理由, 姓氏, 日志说明)。"""
        phone = _extract_phone(comment)
        if not phone:
            return False, PHONE_MISSING, "", "理由里没有手机号"
        try:
            result = await self._member_check(phone, user_id, group_id)
        except Exception as exc:  # noqa: BLE001
            return None, "", "", f"手机号 {_mask_phone(phone)} 校验接口出错：{exc} -> 留给管理员人工处理"
        if result.get("ok") is True:
            return True, "", str(result.get("surname") or ""), f"手机号 {_mask_phone(phone)} 已确认"
        message = str(result.get("message") or "手机号校验未通过")
        return False, message, "", f"手机号 {_mask_phone(phone)} 未通过（{result.get('code')}）"

    # ------------------------------------------------------------------ 客户端
    def _find_bot(self) -> Any:
        pm = getattr(self.context, "platform_manager", None)
        for inst in getattr(pm, "platform_insts", []) or []:
            bot = getattr(inst, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                return bot
        return None

    def _bot_of(self, event: AstrMessageEvent) -> Any:
        return getattr(event, "bot", None) or self._find_bot()

    # -------------------------------------------------------------- 存活检查
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_ciallo(self, event: AstrMessageEvent) -> None:
        """发 /ciallo（大小写均可）时回一句，用来确认机器人还活着。"""
        if not self.cfg.get("ping_enabled", True):
            return

        body = _command_body(event)
        if not body:
            return
        if body.split()[0].lower() != "ciallo":
            return

        # 只在目标群和私聊里响应，避免在无关群里刷屏
        group_id = str(event.get_group_id() or "")
        if group_id and group_id not in self._groups():
            return

        reply = str(self.cfg.get("ping_reply") or _CIALLO_REPLY)
        sender = event.get_sender_id()
        self.logger.info(
            f"{LOG_PREFIX} 存活检查 ciallo from {sender} "
            f"({'私聊' if not group_id else '群 ' + group_id})"
        )
        yield event.plain_result(reply)
        event.stop_event()

    # -------------------------------------------------------------- 群成员同步
    def _ensure_sync_loop(self) -> None:
        """启动定时同步（插件加载时；万一没启动，收到事件时补上）。"""
        if self._sync_task and not self._sync_task.done():
            return
        try:
            self._sync_task = asyncio.get_running_loop().create_task(self._sync_loop())
        except RuntimeError:
            self._sync_task = None  # 还没有事件循环，等下一个事件

    async def _sync_loop(self) -> None:
        await asyncio.sleep(15)  # 等协议端连上
        while True:
            minutes = self._sync_minutes()
            if minutes > 0:
                bot = self._find_bot()
                if bot is not None:
                    for gid in sorted(self._groups()):
                        await self._sync_group(bot, gid)
            await asyncio.sleep(max(minutes, 1) * 60)

    async def _sync_group(self, bot: Any, group_id: str) -> bool:
        """读群成员列表并报给平台。失败只记日志，下次再来。"""
        try:
            res = await bot.call_action("get_group_member_list", group_id=int(group_id))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"{LOG_PREFIX} 群 {group_id} 读取成员失败: {exc}")
            return False
        rows = res.get("data") if isinstance(res, dict) else res
        members = sorted({str(_get(m, "user_id", "")) for m in rows or [] if _get(m, "user_id")})
        if not members:
            self.logger.warning(f"{LOG_PREFIX} 群 {group_id} 成员列表为空，跳过同步")
            return False
        try:
            result = await asyncio.to_thread(
                self._post_sync, "/api/bot/group-members", {"group_id": group_id, "members": members}
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"{LOG_PREFIX} 群 {group_id} 成员同步失败: {exc}")
            return False
        self.logger.info(
            f"{LOG_PREFIX} 群 {group_id} 成员已同步：{len(members)} 人，"
            f"其中 {result.get('linked_users', 0)} 人能对应到平台账号"
        )
        return True

    async def _sync_soon(self, bot: Any, group_id: str, delay: float = 5.0) -> None:
        """有人进群后稍等一下再同步，让他尽快能领取任务。"""
        await asyncio.sleep(delay)
        await self._sync_group(bot, group_id)

    # -------------------------------------------------------------- 入群申请
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_request(self, event: AstrMessageEvent) -> None:
        if not self.cfg.get("enabled", True):
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if _get(raw, "post_type") != "request":
            return
        if _get(raw, "request_type") != "group":
            return
        if _get(raw, "sub_type") != "add":
            return

        group_id = str(_get(raw, "group_id", "") or "")
        user_id = str(_get(raw, "user_id", "") or "")
        flag = _get(raw, "flag")
        comment = str(_get(raw, "comment", "") or "")

        if not group_id or flag is None or group_id not in self._groups():
            return

        bot = self._bot_of(event)
        if bot is None:
            self.logger.error(f"{LOG_PREFIX} 找不到 OneBot 客户端，无法提交审批结果")
            return

        self._ensure_sync_loop()
        approve, reason, surname, why = await self._decide(comment, user_id, group_id)
        if approve is None:
            self.logger.warning(f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} {why}")
            return

        payload: dict[str, Any] = {"flag": flag, "sub_type": "add", "approve": approve}
        if not approve:
            payload["reason"] = reason
        try:
            await bot.call_action("set_group_add_request", **payload)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "already" in text.lower():
                self.logger.info(
                    f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 该申请已被处理过，忽略"
                )
                event.stop_event()
                return
            self.logger.error(
                f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 审批调用失败: {text}"
            )
            return

        if approve:
            now = time.time()
            self._surnames = {
                k: v for k, v in self._surnames.items() if now - v[1] < WELCOME_TTL
            }
            self._surnames[(group_id, user_id)] = (surname, now)
        self.logger.info(
            f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} -> "
            f"{'同意' if approve else '拒绝'}（{why}）"
        )
        event.stop_event()

    # -------------------------------------------------------------- 入群欢迎
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_increase(self, event: AstrMessageEvent) -> None:
        raw = getattr(event.message_obj, "raw_message", None)
        if _get(raw, "post_type") != "notice":
            return
        if _get(raw, "notice_type") != "group_increase":
            return

        group_id = str(_get(raw, "group_id", "") or "")
        user_id = str(_get(raw, "user_id", "") or "")
        if not group_id or not user_id or group_id not in self._groups():
            return
        if user_id == str(_get(raw, "self_id", "") or ""):
            return  # 机器人自己进群

        bot = self._bot_of(event)
        if bot is None:
            return

        self._ensure_sync_loop()
        if self._sync_minutes() > 0:
            self._pending_sync = asyncio.create_task(self._sync_soon(bot, group_id))
        if not self.cfg.get("welcome_enabled", True):
            return

        surname, at = self._surnames.pop((group_id, user_id), ("", 0.0))
        if time.time() - at >= WELCOME_TTL:
            surname = ""
        chain = [
            {"type": "at", "data": {"qq": user_id}},
            {"type": "text", "data": {"text": f" {self._welcome_text(surname)}"}},
        ]
        try:
            await bot.call_action("send_group_msg", group_id=int(group_id), message=chain)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"{LOG_PREFIX} 群 {group_id} 发送欢迎消息失败: {exc}")
            return
        self.logger.info(f"{LOG_PREFIX} 群 {group_id} 欢迎新人 {user_id}")
        event.stop_event()
