"""入群门禁 + 进群验证（OneBot v11 / aiocqhttp）。

两条链路：
1. 入群申请：理由命中「放行理由」则同意，否则拒绝；黑名单用户直接拒绝。
   规则选 phone 模式时，改为从理由里取出学习通手机号，调用平台接口确认
   这个号码在平台里登录过，且没有被别的 QQ 用过。
2. 进群验证：新人进群后 @ 出题（算术），限时回答；
   答对=通过；答错/超时=踢出；累计失败达上限=拉黑（拒绝再次加群 + 本地黑名单）。

未配置的群完全不干预。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

LOG_PREFIX = "[JoinGate]"

KV_FAILURES = "verify_failures"   # {user_id: 累计失败次数}
KV_BLACKLIST = "verify_blacklist"  # [user_id, ...]

PHONE_MISSING = "请在申请理由里填写 11 位学习通手机号"
API_BUSY = "验证服务暂时不可用，请稍后重新申请"

# 协议端会把「入群问题 + 答案」整体塞进 comment，形如：
#   问题：你好
#   答案：1433223
# 需要在匹配前把答案部分取出来，否则 exact 永远匹配不上。
_ANSWER_MARKERS = ("答案：", "答案:", "答案=", "Answer:", "answer:")


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
    """返回用于匹配的候选文本：完整原文，以及从里面抽出的答案部分。"""
    raw = (comment or "").strip()
    cands: list[str] = [raw] if raw else []
    for mark in _ANSWER_MARKERS:
        idx = raw.rfind(mark)
        if idx < 0:
            continue
        tail = raw[idx + len(mark) :].strip()
        if not tail:
            break
        cands.append(tail)
        last_line = tail.splitlines()[-1].strip()
        if last_line:
            cands.append(last_line)
        break

    ordered: list[str] = []
    for cand in cands:
        if cand and cand not in ordered:
            ordered.append(cand)
    return ordered


def _parse_reasons(value: Any) -> list[str]:
    """把多行文本 / 列表解析成理由列表（忽略空行与 # 注释）。"""
    if isinstance(value, str):
        items = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = []
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


_PHONE_RE = re.compile(r"(?<!\d)(?:86)?(1\d{10})(?!\d)")


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


def _normalized_answers(text: str) -> set[str]:
    """把用户回复归一化成候选答案（去空格、去等号、去句未标点）。"""
    base = (text or "").strip().replace(" ", "").replace("　", "")
    out = {base, base.rstrip("。."), base.lstrip("="), base.replace("=", "")}
    return {x for x in out if x}


@dataclass
class Challenge:
    """一次进群验证。"""

    group_id: str
    user_id: str
    a: int
    b: int
    answer: int
    prompt: str
    deadline: float
    task: asyncio.Task | None = field(default=None, repr=False)


class JoinGate(Star):
    """按申请理由自动审批 + 进群做题验证。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        self._pending: dict[tuple[str, str], Challenge] = {}

    async def terminate(self) -> None:
        for ch in list(self._pending.values()):
            if ch.task and not ch.task.done():
                ch.task.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------ 配置
    def _verify_cfg(self) -> dict[str, Any]:
        raw = self.cfg.get("verify") or {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            timeout = max(10, int(raw.get("timeout_seconds", 120)))
        except (TypeError, ValueError):
            timeout = 120
        try:
            operand_max = max(2, int(raw.get("operand_max", 20)))
        except (TypeError, ValueError):
            operand_max = 20
        try:
            max_failures = max(1, int(raw.get("max_failures", 2)))
        except (TypeError, ValueError):
            max_failures = 2
        return {
            "enabled": bool(raw.get("enabled", False)),
            "timeout_seconds": timeout,
            "operand_max": operand_max,
            "max_failures": max_failures,
            "blacklist_on_max": bool(raw.get("blacklist_on_max", True)),
            "kick_on_wrong": bool(raw.get("kick_on_wrong", True)),
        }

    def _rules(self) -> dict[str, dict[str, Any]]:
        rules: dict[str, dict[str, Any]] = {}
        raw_rules = self.cfg.get("rules") or []
        if not isinstance(raw_rules, list):
            return rules
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("group_id") or "").strip()
            if not gid:
                continue
            mode = str(item.get("match_mode") or "exact").strip().lower()
            if mode not in ("exact", "contains", "phone"):
                mode = "exact"
            empty = str(item.get("on_empty_reason") or "reject").strip().lower()
            if empty not in ("reject", "approve"):
                empty = "reject"
            rules[gid] = {
                "reasons": _parse_reasons(item.get("allow_reasons")),
                "match_mode": mode,
                "reject_reason": str(
                    item.get("reject_reason") or "申请理由不正确，请按群公告填写"
                ).strip(),
                "on_empty_reason": empty,
                "verify": bool(item.get("verify_new_member", True)),
            }
        return rules

    def _xxt_cfg(self) -> dict[str, Any]:
        raw = self.cfg.get("xxt") or {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            timeout = min(30.0, max(2.0, float(raw.get("timeout_seconds", 8))))
        except (TypeError, ValueError):
            timeout = 8.0
        on_error = str(raw.get("on_api_error") or "ignore").strip().lower()
        if on_error not in ("ignore", "reject", "approve"):
            on_error = "ignore"
        return {
            "api_base": str(raw.get("api_base") or "").strip().rstrip("/"),
            "api_token": str(raw.get("api_token") or "").strip(),
            "timeout": timeout,
            "on_api_error": on_error,
        }

    @staticmethod
    def _clean_comment(comment: str) -> str:
        """日志用：把多行申请理由压成一行，手机号打码。"""
        text = " ⏎ ".join(
            part.strip() for part in (comment or "").splitlines() if part.strip()
        )
        return re.sub(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)", r"\1****\2", text)

    # -------------------------------------------------------------- KV 状态
    async def _failures(self) -> dict[str, int]:
        data = await self.get_kv_data(KV_FAILURES, {})
        return data if isinstance(data, dict) else {}

    async def _blacklist(self) -> list[str]:
        data = await self.get_kv_data(KV_BLACKLIST, [])
        return data if isinstance(data, list) else []

    async def _is_blacklisted(self, user_id: str) -> bool:
        return str(user_id) in {str(x) for x in await self._blacklist()}

    async def _add_blacklist(self, user_id: str) -> None:
        bl = await self._blacklist()
        if str(user_id) not in {str(x) for x in bl}:
            bl.append(str(user_id))
            await self.put_kv_data(KV_BLACKLIST, bl)

    async def _remove_blacklist(self, user_id: str) -> bool:
        bl = await self._blacklist()
        new = [x for x in bl if str(x) != str(user_id)]
        if len(new) != len(bl):
            await self.put_kv_data(KV_BLACKLIST, new)
            return True
        return False

    # ------------------------------------------------------------------ 判定
    def _decide(self, rule: dict[str, Any], comment: str) -> tuple[bool, str]:
        """纯函数式判定，便于单元测试。返回 (是否放行, 原因说明)。"""
        comment = (comment or "").strip()
        if not comment:
            if rule.get("on_empty_reason") == "approve":
                return True, "未填写理由 -> 按配置放行"
            return False, "未填写申请理由"

        reasons: list[str] = rule.get("reasons") or []
        mode = rule.get("match_mode")
        if mode == "phone":
            # 干跑：只看能不能取到手机号，真正放行要等接口确认
            phone = _extract_phone(comment)
            if phone:
                return True, f"取到手机号 {_mask_phone(phone)}（实际放行还要接口确认）"
            return False, PHONE_MISSING

        if not reasons:
            return False, "该群未配置放行理由"

        candidates = _answer_candidates(comment)

        if mode == "contains":
            for cand in candidates:
                for reason in reasons:
                    if reason in cand:
                        how = "完整原文" if cand == comment else "抽取的答案"
                        return True, f"命中关键词「{reason}」（{how}）"
        else:
            for cand in candidates:
                for reason in reasons:
                    if cand == reason:
                        how = "完整原文" if cand == comment else "抽取的答案"
                        return True, f"理由完全匹配（{how}）"
        return False, "理由不在放行名单"

    # ------------------------------------------------------------ 平台接口
    def _xxt_request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """同步调用平台接口（放在线程里跑）。失败抛 RuntimeError。"""
        xcfg = self._xxt_cfg()
        if not xcfg["api_base"] or not xcfg["api_token"]:
            raise RuntimeError("未配置平台接口地址或密钥")
        req = urllib.request.Request(
            xcfg["api_base"] + path,
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {xcfg['api_token']}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=xcfg["timeout"]) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").strip()[:120]
            raise RuntimeError(f"HTTP {exc.code} {body}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    async def _member_check(self, phone: str, qq: str, group_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._xxt_request,
            "POST",
            "/api/bot/member-check",
            {"phone": phone, "qq": qq, "group_id": group_id},
        )

    async def _unbind(self, target: str) -> dict[str, Any]:
        key = "phone" if _extract_phone(target) == target else "qq"
        return await asyncio.to_thread(
            self._xxt_request, "DELETE", "/api/bot/member-binding", {key: target}
        )

    async def _decide_phone(
        self, rule: dict[str, Any], comment: str, user_id: str, group_id: str
    ) -> tuple[bool | None, str, str]:
        """phone 模式判定。返回 (放行? None=留给人工, 回给申请人的理由, 日志说明)。"""
        if not comment.strip() and rule.get("on_empty_reason") == "approve":
            return True, "", "未填写理由 -> 按配置放行"
        phone = _extract_phone(comment)
        if not phone:
            return False, PHONE_MISSING, "理由里没有手机号"
        try:
            result = await self._member_check(phone, user_id, group_id)
        except Exception as exc:  # noqa: BLE001
            on_error = self._xxt_cfg()["on_api_error"]
            why = f"手机号 {_mask_phone(phone)} 校验接口出错：{exc}"
            if on_error == "approve":
                return True, "", why + " -> 按配置放行"
            if on_error == "reject":
                return False, API_BUSY, why + " -> 按配置拒绝"
            return None, "", why + " -> 留给管理员人工处理"
        code = str(result.get("code") or "")
        if result.get("ok") is True:
            return True, "", f"手机号 {_mask_phone(phone)} 已确认"
        message = str(result.get("message") or rule["reject_reason"])
        return False, message, f"手机号 {_mask_phone(phone)} 未通过（{code}）"

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

    async def _send_at(self, bot: Any, group_id: str, user_id: str, text: str) -> None:
        """@ 某人并发送文本（OneBot v11 消息段数组）。"""
        chain = [
            {"type": "at", "data": {"qq": str(user_id)}},
            {"type": "text", "data": {"text": f" {text}"}},
        ]
        try:
            await bot.call_action("send_group_msg", group_id=int(group_id), message=chain)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"{LOG_PREFIX} 群 {group_id} 发送 @ 消息失败: {exc}")

    async def _kick(self, bot: Any, group_id: str, user_id: str, block: bool) -> bool:
        try:
            await bot.call_action(
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=block,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "not" in text.lower() and "member" in text.lower():
                self.logger.info(
                    f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 已不在群里，无需踢出"
                )
            else:
                self.logger.error(
                    f"{LOG_PREFIX} 群 {group_id} 踢出 {user_id} 失败: {text}"
                )
            return False

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

        if not group_id or flag is None:
            return

        rule = self._rules().get(group_id)
        if rule is None:
            return  # 非目标群：不处理

        bot = self._bot_of(event)
        if bot is None:
            self.logger.error(f"{LOG_PREFIX} 找不到 OneBot 客户端，无法提交审批结果")
            return

        # 黑名单：直接拒绝
        if user_id and await self._is_blacklisted(user_id):
            try:
                await bot.call_action(
                    "set_group_add_request",
                    flag=flag,
                    sub_type="add",
                    approve=False,
                    reason="该账号已被列入黑名单",
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"{LOG_PREFIX} 黑名单拒绝失败: {exc}")
                return
            self.logger.info(
                f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 在黑名单中 -> 拒绝"
            )
            event.stop_event()
            return

        if rule["match_mode"] == "phone":
            approve, reject_text, why = await self._decide_phone(
                rule, comment, user_id, group_id
            )
            if approve is None:
                self.logger.warning(
                    f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} {why}"
                )
                return
        else:
            approve, why = self._decide(rule, comment)
            reject_text = rule["reject_reason"]
        payload: dict[str, Any] = {
            "flag": flag,
            "sub_type": "add",
            "approve": approve,
        }
        if not approve:
            payload["reason"] = reject_text

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

        action = "同意" if approve else "拒绝"
        self.logger.info(
            f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} "
            f"理由「{self._clean_comment(comment)}」-> {action}（{why}）"
        )
        event.stop_event()

    # -------------------------------------------------------------- 进群验证
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_increase(self, event: AstrMessageEvent) -> None:
        vcfg = self._verify_cfg()
        if not vcfg["enabled"]:
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if _get(raw, "post_type") != "notice":
            return
        if _get(raw, "notice_type") != "group_increase":
            return

        group_id = str(_get(raw, "group_id", "") or "")
        user_id = str(_get(raw, "user_id", "") or "")
        if not group_id or not user_id:
            return

        rule = self._rules().get(group_id)
        if rule is None or not rule.get("verify"):
            return

        bot = self._bot_of(event)
        if bot is None:
            return

        # 黑名单用户混进来了：直接踢
        if await self._is_blacklisted(user_id):
            await self._kick(bot, group_id, user_id, block=True)
            self.logger.info(
                f"{LOG_PREFIX} 群 {group_id} 黑名单用户 {user_id} 进群 -> 直接踢出"
            )
            return

        if (group_id, user_id) in self._pending:
            return  # 已有进行中的验证，不重复出题

        a = random.randint(1, vcfg["operand_max"])
        b = random.randint(1, vcfg["operand_max"])
        answer = a + b
        timeout = vcfg["timeout_seconds"]
        prompt = f"请在 {timeout} 秒内回答：{a} + {b} = ?（只回复数字，答错会被移出群聊）"

        ch = Challenge(
            group_id=group_id,
            user_id=user_id,
            a=a,
            b=b,
            answer=answer,
            prompt=prompt,
            deadline=time.time() + timeout,
        )
        self._pending[(group_id, user_id)] = ch
        ch.task = asyncio.create_task(self._timeout_watch(ch))

        await self._send_at(bot, group_id, user_id, prompt)
        self.logger.info(
            f"{LOG_PREFIX} 群 {group_id} 新人 {user_id} 进群，出题 {a}+{b}={answer}，"
            f"限时 {timeout}s"
        )
        event.stop_event()

    async def _timeout_watch(self, ch: Challenge) -> None:
        """到点未通过则视为失败。"""
        try:
            await asyncio.sleep(max(0.0, ch.deadline - time.time()))
            key = (ch.group_id, ch.user_id)
            if self._pending.get(key) is not ch:
                return
            self._pending.pop(key, None)
            bot = self._find_bot()
            if bot is None:
                return
            await self._fail(bot, ch.group_id, ch.user_id, "超时未回答")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"{LOG_PREFIX} 超时处理异常: {exc}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_verify_answer(self, event: AstrMessageEvent) -> None:
        if not self._pending:
            return

        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        key = (group_id, user_id)
        ch = self._pending.get(key)
        if ch is None:
            return

        text = (event.message_str or "").strip()
        if not text or text.startswith(("/", "／")):
            return  # 管理员命令不当作答案

        # 只在「看起来像答案」的消息上判定，避免闲聊/表情被误判踢人
        if not any(ch_ in "0123456789" for ch_ in text) and not text.startswith("-"):
            return

        normalized = _normalized_answers(text)
        if str(ch.answer) in normalized:
            self._pending.pop(key, None)
            if ch.task and not ch.task.done():
                ch.task.cancel()
            bot = self._bot_of(event)
            if bot is not None:
                await self._send_at(bot, group_id, user_id, "回答正确，欢迎加入。")
            self.logger.info(
                f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 答对 {ch.answer} -> 通过"
            )
            event.stop_event()
            return

        self._pending.pop(key, None)
        if ch.task and not ch.task.done():
            ch.task.cancel()
        bot = self._bot_of(event)
        if bot is not None:
            await self._fail(bot, group_id, user_id, f"回答错误（应回答 {ch.answer}）")
        event.stop_event()

    async def _fail(
        self, bot: Any, group_id: str, user_id: str, reason: str
    ) -> None:
        """记一次失败：达上限则拉黑，然后踢出。"""
        vcfg = self._verify_cfg()
        failures = await self._failures()
        count = int(failures.get(user_id, 0)) + 1
        failures[user_id] = count
        await self.put_kv_data(KV_FAILURES, failures)

        blacklisted = False
        if vcfg["blacklist_on_max"] and count >= vcfg["max_failures"]:
            await self._add_blacklist(user_id)
            blacklisted = True

        kicked = False
        if vcfg["kick_on_wrong"]:
            kicked = await self._kick(bot, group_id, user_id, block=blacklisted)

        if kicked:
            if blacklisted:
                await self._send_at(
                    bot,
                    group_id,
                    user_id,
                    f"{reason}，已累计失败 {count} 次，账号已拉黑并移出群聊。",
                )
            else:
                await self._send_at(
                    bot,
                    group_id,
                    user_id,
                    f"{reason}，已移出群聊（累计失败 {count} 次，"
                    f"达 {vcfg['max_failures']} 次将被拉黑）。",
                )

        self.logger.info(
            f"{LOG_PREFIX} 群 {group_id} 用户 {user_id} 验证失败：{reason}；"
            f"累计 {count}/{vcfg['max_failures']}"
            f"{'；已拉黑' if blacklisted else ''}"
        )

    # -------------------------------------------------------------- 管理员命令
    def _admins(self) -> set[str]:
        raw = self.cfg.get("admin_qq") or ""
        if isinstance(raw, (list, tuple)):
            items = [str(x) for x in raw]
        else:
            items = str(raw).replace("，", "\n").replace(",", "\n").splitlines()
        return {x.strip() for x in items if x.strip()}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self._admins()

    @staticmethod
    def _parse_duration(token: str | None, default: int = 600) -> int:
        """支持 600 / 600s / 10m / 2h / 1d / 永久。"""
        if not token:
            return default
        t = token.strip().lower()
        if t in ("永久", "forever", "max"):
            return 2592000  # QQ 上限 30 天
        unit = 1
        for suffix, mult in (("s", 1), ("秒", 1), ("m", 60), ("分", 60),
                             ("h", 3600), ("小时", 3600), ("d", 86400), ("天", 86400)):
            if t.endswith(suffix):
                t = t[: -len(suffix)]
                unit = mult
                break
        try:
            value = int(float(t))
        except (TypeError, ValueError):
            return default
        return max(0, min(value * unit, 2592000))

    @staticmethod
    def _human_duration(seconds: int) -> str:
        if seconds <= 0:
            return "0 秒"
        if seconds % 86400 == 0:
            return f"{seconds // 86400} 天"
        if seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    def _resolve_group_target(
        self, event: AstrMessageEvent, args: list[str]
    ) -> tuple[str | None, str | None, list[str]]:
        """群里：/<命令> <QQ> [参数...]；私聊：/<命令> <群号> <QQ> [参数...]。"""
        if event.is_private_chat():
            if len(args) < 2:
                return None, None, []
            return args[0].strip(), args[1].strip(), args[2:]
        gid = str(event.get_group_id() or "")
        if not args:
            return gid or None, None, []
        return gid or None, args[0].strip(), args[1:]

    async def _ban(self, bot: Any, group_id: str, user_id: str, seconds: int) -> bool:
        try:
            await bot.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=int(seconds),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                f"{LOG_PREFIX} 群 {group_id} 禁言 {user_id} 失败: {exc}"
            )
            return False

    ADMIN_USAGE = (
        "群管命令（管理员）\n"
        "群聊：/命令 <QQ> [参数]\n"
        "私聊：/命令 <群号> <QQ> [参数]\n\n"
        "直接拉黑：/拉黑 <QQ>（私聊可写成 /拉黑 <群号> <QQ>）\n"
        "解除拉黑：/解黑 <QQ>\n"
        "查看黑名单：/黑名单\n"
        "禁言：/禁言 <QQ> [时长 600|10m|2h|1d|永久]\n"
        "解除禁言：/解禁 <QQ>\n"
        "踢出：/踢 <QQ> [黑]\n"
        "解绑手机号：/解绑 <QQ或手机号>（换了 QQ 时用）\n"
        "查看本插件规则：/joingate"
    )

    @staticmethod
    def _command_body(event: AstrMessageEvent) -> str | None:
        """提取 /命令 后面的内容。

        注意：AstrBot 的唤醒阶段会把 wake_prefix（默认 "/"）从
        event.message_str 里删掉，所以不能只看 message_str，
        必须回看原始消息（raw_message）才能判断是否带了斜杠。
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

        if event.message_str:
            candidates.append(event.message_str)

        for cand in candidates:
            c = cand.strip()
            # 去掉 @ 之类的 CQ 码前缀，例如 "[CQ:at,qq=123] /禁言 456"
            c = re.sub(r"^(?:\[CQ:[^\]]+\])+", "", c).strip()
            if c.startswith(("/", "／")):
                return c[1:].strip()
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_admin_command(self, event: AstrMessageEvent) -> None:
        body = self._command_body(event)
        if not body:
            return

        parts = body.split()
        cmd = parts[0]
        args = parts[1:]

        known = {"拉黑", "解黑", "黑名单", "禁言", "解禁", "踢", "解绑", "群管", "管理", "帮助"}
        if cmd not in known:
            return

        if not self._is_admin(event):
            self.logger.info(
                f"{LOG_PREFIX} 非管理员 {event.get_sender_id()} 尝试执行 /{cmd}，已忽略"
            )
            return

        self.logger.info(
            f"{LOG_PREFIX} 管理员 {event.get_sender_id()} 执行 /{cmd} {args}"
        )

        bot = self._bot_of(event)
        if bot is None:
            yield event.plain_result("找不到 OneBot 客户端")
            return

        if cmd in ("群管", "管理", "帮助"):
            yield event.plain_result(self.ADMIN_USAGE)
            event.stop_event()
            return

        if cmd == "黑名单":
            bl = await self._blacklist()
            yield event.plain_result(
                "黑名单：\n" + "\n".join(f"- {x}" for x in bl) if bl else "黑名单为空"
            )
            event.stop_event()
            return

        if cmd == "解绑":
            # 不需要群号：取最后一个参数（QQ 或手机号）
            target = args[-1].strip() if args else ""
            if not target.isdigit():
                yield event.plain_result("用法：/解绑 <QQ号或手机号>")
                return
            try:
                result = await self._unbind(target)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"解绑失败：{exc}")
                return
            shown = _mask_phone(target) if _extract_phone(target) == target else target
            if result.get("removed"):
                yield event.plain_result(
                    f"已解绑 {shown}（原绑定 QQ {result.get('qq')}），可以重新申请了"
                )
            else:
                yield event.plain_result(f"{shown} 没有绑定记录")
            event.stop_event()
            return

        group_id, target, rest = self._resolve_group_target(event, args)
        if cmd in ("拉黑", "解黑"):
            # 拉黑不强制需要群号：取最后一个纯数字参数作为 QQ
            if args and args[-1].isdigit() and len(args[-1]) >= 5:
                target = args[-1]
            elif args:
                target = args[0]
            if not target:
                yield event.plain_result(
                    f"用法：/{cmd} <QQ号>（私聊也可 /{cmd} <群号> <QQ号>）"
                )
                return
            if cmd == "拉黑":
                await self._add_blacklist(target)
                msg = f"已拉黑 {target}"
                if not event.is_private_chat() and group_id:
                    if await self._kick(bot, group_id, target, block=True):
                        msg += "，并已踢出本群"
                yield event.plain_result(msg)
            else:
                ok = await self._remove_blacklist(target)
                yield event.plain_result(
                    f"已解除拉黑 {target}" if ok else f"{target} 不在黑名单里"
                )
            event.stop_event()
            return

        if not group_id or not target:
            yield event.plain_result(
                f"用法：/{cmd} <QQ> [参数]（私聊需先填群号）"
            )
            return

        if cmd == "禁言":
            seconds = self._parse_duration(rest[0] if rest else None, default=600)
            ok = await self._ban(bot, group_id, target, seconds)
            yield event.plain_result(
                f"已禁言 {target} {self._human_duration(seconds)}"
                if ok
                else f"禁言 {target} 失败，请检查权限与参数"
            )
        elif cmd == "解禁":
            ok = await self._ban(bot, group_id, target, 0)
            yield event.plain_result(
                f"已解除 {target} 的禁言" if ok else f"解除禁言失败"
            )
        elif cmd == "踢":
            block = bool(rest) and rest[0] in ("黑", "blacklist", "1", "true")
            ok = await self._kick(bot, group_id, target, block=block)
            yield event.plain_result(
                f"已踢出 {target}" + ("（并拉黑）" if block else "")
                if ok
                else f"踢出 {target} 失败"
            )
        event.stop_event()

    # ------------------------------------------------------------------ 内部命令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("joingate")
    async def joingate(self, event: AstrMessageEvent):
        """用法：/joingate [status|test <群号> <理由>|fails|banlist|ban <QQ>|unban <QQ>|reset <QQ>]"""
        text = (event.message_str or "").strip()
        for prefix in ("/joingate", "joingate"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break

        parts = text.split(maxsplit=2)
        sub = parts[0].lower() if parts and parts[0] else "status"
        arg1 = parts[1].strip() if len(parts) > 1 else ""
        arg2 = parts[2].strip() if len(parts) > 2 else ""

        rules = self._rules()
        vcfg = self._verify_cfg()

        if sub == "test":
            if not arg1 or not arg2:
                yield event.plain_result("用法：/joingate test <群号> <申请理由>")
                return
            rule = rules.get(arg1)
            if rule is None:
                yield event.plain_result(f"群 {arg1} 不在规则里（不会处理）")
                return
            approve, why = self._decide(rule, arg2)
            yield event.plain_result(
                f"群 {arg1}\n理由：{arg2}\n判定：{'放行' if approve else '拒绝'}\n依据：{why}"
            )
            return

        if sub == "ban":
            if not arg1:
                yield event.plain_result("用法：/joingate ban <QQ号>")
                return
            await self._add_blacklist(arg1)
            yield event.plain_result(f"已加入黑名单：{arg1}")
            return

        if sub == "unban":
            if not arg1:
                yield event.plain_result("用法：/joingate unban <QQ号>")
                return
            ok = await self._remove_blacklist(arg1)
            yield event.plain_result(
                f"已移出黑名单：{arg1}" if ok else f"{arg1} 不在黑名单里"
            )
            return

        if sub == "reset":
            if not arg1:
                yield event.plain_result("用法：/joingate reset <QQ号>")
                return
            failures = await self._failures()
            if arg1 in failures:
                failures.pop(arg1)
                await self.put_kv_data(KV_FAILURES, failures)
            await self._remove_blacklist(arg1)
            yield event.plain_result(f"已重置 {arg1} 的失败次数与黑名单状态")
            return

        if sub == "banlist":
            bl = await self._blacklist()
            yield event.plain_result(
                "黑名单：\n" + "\n".join(f"- {x}" for x in bl) if bl else "黑名单为空"
            )
            return

        if sub == "fails":
            failures = await self._failures()
            if not failures:
                yield event.plain_result("暂无失败记录")
                return
            lines = [f"{uid}: {cnt} 次" for uid, cnt in sorted(failures.items())]
            yield event.plain_result("验证失败记录：\n" + "\n".join(lines))
            return

        # status
        if not rules:
            yield event.plain_result(
                "尚未配置规则。请在 WebUI → 插件管理 → 入群申请理由门禁 里添加。"
            )
            return
        lines = [
            f"规则 {len(rules)} 个 | 验证：{'开' if vcfg['enabled'] else '关'}"
            f" | 限时 {vcfg['timeout_seconds']}s | 上限 {vcfg['max_failures']} 次"
        ]
        for gid, rule in rules.items():
            mode = {"contains": "包含", "phone": "手机号校验"}.get(
                rule["match_mode"], "完全匹配"
            )
            lines.append(
                f"- {gid}｜{mode}｜{len(rule['reasons'])} 条理由"
                f"｜验证:{'开' if rule['verify'] else '关'}"
                f"｜空理由:{'放行' if rule['on_empty_reason'] == 'approve' else '拒绝'}"
            )
        if any(r["match_mode"] == "phone" for r in rules.values()):
            xcfg = self._xxt_cfg()
            ready = bool(xcfg["api_base"] and xcfg["api_token"])
            on_error = {"ignore": "留给人工", "reject": "拒绝", "approve": "放行"}
            lines.append(
                f"平台接口：{xcfg['api_base'] or '未配置'}"
                f"｜密钥:{'已配置' if xcfg['api_token'] else '未配置'}"
                f"｜出错时:{on_error[xcfg['on_api_error']]}"
                + ("" if ready else "｜手机号校验无法工作，请先配置")
            )
        yield event.plain_result("\n".join(lines))
