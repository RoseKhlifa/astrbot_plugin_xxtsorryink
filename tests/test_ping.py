"""自测：/ciallo 与裸 ciallo 存活检查。"""
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
_body = mod._command_body
_plain = mod._plain_text
_is = mod._is_ciallo
CIALLO = mod._CIALLO_REPLY

assert CIALLO == "Ciallo～(∠・ω< )⌒★", CIALLO


class MsgObj:
    def __init__(self, raw):
        self.raw_message = raw


class Res:
    def __init__(self, text):
        self.text = text


class Ev:
    def __init__(self, raw, message_str="", group="1081510461", sender="2221542777"):
        self.message_obj = MsgObj(raw)
        self.message_str = message_str
        self._group = group
        self._sender = sender
        self.stopped = False

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def plain_result(self, text):
        return Res(text)

    def stop_event(self):
        self.stopped = True


def make_gate(**extra):
    g = JoinGate.__new__(JoinGate)
    g.cfg = {
        "enabled": True,
        "group_ids": "1081510461",
        "api_base": "http://127.0.0.1:1",
        "api_token": "x",
        **extra,
    }
    g._surnames = {}
    g._logs = []
    g.logger = type(
        "L", (), {
            "info": lambda self, m: g._logs.append(m),
            "error": lambda self, m: g._logs.append(m),
            "warning": lambda self, m: g._logs.append(m),
        }
    )()
    return g


async def run(g, ev):
    out = []
    async for r in g.on_ciallo(ev):
        out.append(r)
    return out


async def main():
    # ---- 文本提取 ----
    assert _body(Ev({"raw_message": "/ciallo"}, message_str="ciallo")) == "ciallo"
    assert _body(Ev({"raw_message": "ciallo"}, message_str="ciallo")) is None
    assert _plain(Ev({"raw_message": "ciallo"}, message_str="ciallo")) == "ciallo"
    assert _body(Ev({"raw_message": "[CQ:at,qq=1] ciallo"}, message_str="ciallo")) is None
    assert _plain(Ev({"raw_message": "[CQ:at,qq=1] ciallo"}, message_str="ciallo")) == "ciallo"
    assert _body(Ev({"raw_message": "／CIALLO"}, message_str="CIALLO")) == "CIALLO"
    print("OK  文本提取（斜杠 / 裸文本 / 全角 / CQ 前缀）")

    # ---- 裸命令判定 ----
    assert _is("ciallo", whole=True) is True
    assert _is("Ciallo", whole=True) is True
    assert _is("  CIALLO  ", whole=True) is True
    assert _is("ciallo！", whole=True) is True
    assert _is("ciallo~", whole=True) is True
    assert _is("ciallo 是什么意思", whole=True) is False
    assert _is("cialloooo", whole=True) is False
    assert _is("hi ciallo", whole=True) is False
    print("OK  裸命令严格判定（标点可容忍，长句不误触）")

    # ---- 斜杠命令允许带参数 ----
    assert _is("ciallo 你好", whole=False) is True
    assert _is("Ciallo!", whole=False) is True
    assert _is("other", whole=False) is False
    print("OK  斜杠命令取首词")

    g = make_gate()

    # ---- 群里裸发 / 带斜杠都响应 ----
    for text in ("ciallo", "Ciallo", "CIALLO", "ciallo！", "/ciallo", "/Ciallo"):
        ev = Ev({"raw_message": text}, message_str=text.lstrip("/"))
        out = await run(g, ev)
        assert len(out) == 1 and out[0].text == CIALLO, (text, out)
        assert ev.stopped
    print("OK  群里裸发与 /ciallo 都响应，且 stop_event")

    # ---- 误触保护 ----
    for text in ("ciallo 是什么意思", "ciallooo", "今天 ciallo 了吗", "/help"):
        ev = Ev({"raw_message": text}, message_str=text.lstrip("/"))
        assert not await run(g, ev), text
    print("OK  长句/其他命令不误触")

    # ---- 私聊 ----
    for text in ("ciallo", "/ciallo"):
        ev = Ev({"raw_message": text}, message_str=text.lstrip("/"), group=None)
        out = await run(g, ev)
        assert len(out) == 1, text
    print("OK  私聊裸发与 /ciallo 都响应")

    # ---- 非目标群 / 开关 / 自定义回复 ----
    ev = Ev({"raw_message": "ciallo"}, message_str="ciallo", group="999999")
    assert not await run(g, ev)
    ev = Ev({"raw_message": "ciallo"}, message_str="ciallo")
    assert not await run(make_gate(ping_enabled=False), ev)
    out = await run(make_gate(ping_reply="pong"), Ev({"raw_message": "ciallo"}, message_str="ciallo"))
    assert out[0].text == "pong"
    print("OK  非目标群不响应 / 开关 / 自定义回复")

    assert any("存活检查" in m for m in g._logs)
    print("OK  日志留痕")

    print("\nALL PASSED")


asyncio.run(main())
