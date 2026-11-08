"""管理员命令逻辑自测：时长解析 / 管理员识别 / 禁言与踢人参数。"""
import asyncio
import importlib.util
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("jg", _PLUGIN)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

JoinGate = mod.JoinGate


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {}


def make_gate(admin_qq="2221542777"):
    g = JoinGate.__new__(JoinGate)
    g.cfg = {"enabled": True, "admin_qq": admin_qq, "rules": []}
    g._pending = {}
    g._mem = {}

    async def get_kv(key, default=None):
        return g._mem.get(key, default)

    async def put_kv(key, value):
        g._mem[key] = value

    g.get_kv_data = get_kv
    g.put_kv_data = put_kv
    g.logger = type(
        "L", (), {
            "info": lambda *a, **k: None,
            "error": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
        }
    )()
    return g


class FakeEvent:
    def __init__(self, sender="2221542777", group="2163040959", private=False):
        self._sender = sender
        self._group = group
        self._private = private

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return None if self._private else self._group

    def is_private_chat(self):
        return self._private


class FakeMsgObj:
    def __init__(self, raw):
        self.raw_message = raw


class CmdEvent(FakeEvent):
    """带 message_obj / message_str 的命令事件。"""

    def __init__(self, raw=None, message_str="", **kw):
        super().__init__(**kw)
        self.message_obj = FakeMsgObj(raw)
        self.message_str = message_str


async def main():
    g = make_gate()

    # ---- 命令文本提取（关键：唤醒阶段会删掉开头的 /） ----
    body = g._command_body
    assert body(CmdEvent({"raw_message": "/管理"}, message_str="管理")) == "管理"
    assert body(CmdEvent({"raw_message": "/禁言 123456 10m"}, message_str="禁言 123456 10m")) == "禁言 123456 10m"
    assert body(CmdEvent({"raw_message": "／拉黑 999999"}, message_str="拉黑 999999")) == "拉黑 999999"
    # @ 之后的 CQ 码前缀
    assert body(CmdEvent({"raw_message": "[CQ:at,qq=1] /踢 123456"}, message_str="踢 123456")) == "踢 123456"
    # array 格式
    assert body(CmdEvent({"message": [{"type": "text", "data": {"text": "/黑名单"}}]}, message_str="黑名单")) == "黑名单"
    # 没有斜杠的普通聊天 -> 不当作命令
    assert body(CmdEvent({"raw_message": "管理"}, message_str="管理")) is None
    assert body(CmdEvent({"raw_message": "今天管理得不错"}, message_str="今天管理得不错")) is None
    print("OK  命令文本提取（含唤醒前缀剥离 / 全角斜杠 / CQ 码 / array / 误触保护）")

    # ---- 管理员识别 ----
    assert g._admins() == {"2221542777"}, g._admins()
    assert g._is_admin(FakeEvent("2221542777")) is True
    assert g._is_admin(FakeEvent("10001")) is False
    print("OK  管理员识别（仅 2221542777）")

    # 多管理员 / 中文逗号
    g2 = make_gate("2221542777，10086\n10010")
    assert g2._admins() == {"2221542777", "10086", "10010"}, g2._admins()
    print("OK  多管理员解析（逗号/换行/中文逗号）")

    # ---- 时长解析 ----
    p = g._parse_duration
    assert p("600") == 600
    assert p("10m") == 600
    assert p("2h") == 7200
    assert p("1d") == 86400
    assert p("30秒") == 30
    assert p("永久") == 2592000
    assert p(None) == 600
    assert p("abc") == 600          # 非法值回退默认
    assert p("999d") == 2592000     # 上限裁剪
    print("OK  时长解析（600/10m/2h/1d/30秒/永久/非法/上限）")

    # ---- 人类可读 ----
    assert g._human_duration(600) == "10 分钟"
    assert g._human_duration(7200) == "2 小时"
    assert g._human_duration(86400) == "1 天"
    assert g._human_duration(45) == "45 秒"
    assert g._human_duration(0) == "0 秒"
    print("OK  时长格式化")

    # ---- 禁言 / 解禁 参数 ----
    bot = FakeBot()
    await g._ban(bot, "2163040959", "10001", 600)
    act, kw = bot.calls[-1]
    assert act == "set_group_ban"
    assert kw == {"group_id": 2163040959, "user_id": 10001, "duration": 600}, kw
    await g._ban(bot, "2163040959", "10001", 0)
    assert bot.calls[-1][1]["duration"] == 0
    print("OK  set_group_ban 参数正确（含解禁 duration=0）")

    # ---- 踢人 + 拉黑标记 ----
    await g._kick(bot, "2163040959", "10002", block=True)
    act, kw = bot.calls[-1]
    assert act == "set_group_kick"
    assert kw["reject_add_request"] is True
    await g._kick(bot, "2163040959", "10003", block=False)
    assert bot.calls[-1][1]["reject_add_request"] is False
    print("OK  set_group_kick 的 reject_add_request 正确")

    # ---- 拉黑 / 解黑 与命令共用存储 ----
    await g._add_blacklist("10004")
    assert await g._is_blacklisted("10004") is True
    assert await g._remove_blacklist("10004") is True
    assert await g._is_blacklisted("10004") is False
    print("OK  拉黑/解黑 与入群申请黑名单共用同一存储")

    print("\nALL PASSED")


asyncio.run(main())
