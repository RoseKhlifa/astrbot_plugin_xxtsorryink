"""自测：/ciallo 存活检查（命令提取 + 作用域 + 大小写 + 自定义回复）。"""
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
    # ---- 命令文本提取（唤醒阶段会删掉开头的 /）----
    assert _body(Ev({"raw_message": "/ciallo"}, message_str="ciallo")) == "ciallo"
    assert _body(Ev({"raw_message": "/Ciallo"}, message_str="Ciallo")) == "Ciallo"
    assert _body(Ev({"raw_message": "／CIALLO"}, message_str="CIALLO")) == "CIALLO"
    assert _body(Ev({"raw_message": "[CQ:at,qq=1] /ciallo"}, message_str="ciallo")) == "ciallo"
    assert _body(Ev({"message": [{"type": "text", "data": {"text": "/ciallo"}}]},
                    message_str="ciallo")) == "ciallo"
    assert _body(Ev({"raw_message": "ciallo"}, message_str="ciallo")) is None, "无斜杠不算命令"
    print("OK  命令文本提取（前缀剥离 / 全角 / CQ 前缀 / array / 无斜杠不触发）")

    g = make_gate()

    # ---- 正常响应 ----
    for raw in ("/ciallo", "/Ciallo", "/CIALLO", "/CiAlLo"):
        ev = Ev({"raw_message": raw}, message_str=raw[1:])
        out = await run(g, ev)
        assert len(out) == 1 and out[0].text == CIALLO, (raw, out)
        assert ev.stopped, "必须 stop_event，避免继续交给 LLM"
    print("OK  大小写不敏感，回复内容精确匹配，且 stop_event")

    # ---- 私聊也响应 ----
    ev = Ev({"raw_message": "/ciallo"}, message_str="ciallo", group=None)
    out = await run(g, ev)
    assert len(out) == 1, "私聊应响应"
    print("OK  私聊响应")

    # ---- 非目标群不响应 ----
    ev = Ev({"raw_message": "/ciallo"}, message_str="ciallo", group="999999")
    out = await run(g, ev)
    assert not out and not ev.stopped, "非目标群不应响应"
    print("OK  非目标群不响应")

    # ---- 其他命令不响应 ----
    ev = Ev({"raw_message": "/help"}, message_str="help")
    assert not await run(g, ev)
    print("OK  其他命令不响应")

    # ---- 关闭开关 ----
    ev = Ev({"raw_message": "/ciallo"}, message_str="ciallo")
    assert not await run(make_gate(ping_enabled=False), ev)
    print("OK  ping_enabled=false 时不响应")

    # ---- 自定义回复 ----
    ev = Ev({"raw_message": "/ciallo"}, message_str="ciallo")
    out = await run(make_gate(ping_reply="pong"), ev)
    assert out[0].text == "pong"
    print("OK  自定义 ping_reply")

    # ---- 日志留痕（便于确认存活）----
    assert any("存活检查" in m for m in g._logs), g._logs
    print("OK  日志留痕")

    print("\nALL PASSED")


asyncio.run(main())
