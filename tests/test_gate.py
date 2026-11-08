"""自测：手机号提取 + 审批判定 + 真实 HTTP 调用（本地假接口）+ 入群欢迎。"""
import asyncio
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("jg", _PLUGIN)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

JoinGate = mod.JoinGate
extract = mod._extract_phone

# ---- 手机号提取 ----
cases = [
    ("问题：请填写学习通手机号\n答案：13800000001", "13800000001"),
    ("答案：138 0000 0001", "13800000001"),
    ("答案：138-0000-0001", "13800000001"),
    ("答案：+86 13800000001", "13800000001"),
    ("答案：８６１３８０００００００１", "13800000001"),  # 全角
    ("我的手机号是13800000001谢谢", "13800000001"),
    ("13800000001", "13800000001"),
    ("答案：1380000000", None),        # 10 位
    ("答案：138000000012", None),      # 12 位
    ("答案：23800000001", None),       # 不是 1 开头
    ("问题：手机号\n答案：", None),
    ("", None),
]
for comment, want in cases:
    got = extract(comment)
    assert got == want, (comment, got, want)
assert mod._mask_phone("13800000001") == "138****0001"
assert mod._parse_groups("123456\n 654321 ,abc\n\n") == {"123456", "654321"}


# ---- 本地假接口 ----
class FakeAPI(BaseHTTPRequestHandler):
    calls = []
    fail = False

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.calls.append((self.path, self.headers.get("Authorization"), body))
        if FakeAPI.fail:
            self._reply(500, {"error": "boom"})
        elif self.headers.get("Authorization") != "Bearer secret":
            self._reply(401, {"error": "unauthorized"})
        elif body["phone"] == "13800000001":
            self._reply(200, {"ok": True, "code": "ok", "surname": "张"})
        elif body["phone"] == "13800000002":
            self._reply(200, {"ok": True, "code": "ok", "surname": ""})
        else:
            self._reply(200, {"ok": False, "code": "not_found", "message": "这个手机号还没有在平台登录过，请先登录后再申请"})

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), FakeAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"


class Bot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))


class Event:
    def __init__(self, raw):
        self.message_obj = type("M", (), {"raw_message": raw})()
        self.bot = Bot()
        self.stopped = False

    def stop_event(self):
        self.stopped = True


def request(user_id, comment, group_id=1):
    return Event({"post_type": "request", "request_type": "group", "sub_type": "add",
                  "group_id": group_id, "user_id": user_id, "flag": f"f{user_id}", "comment": comment})


def joined(user_id, group_id=1, self_id=999):
    return Event({"post_type": "notice", "notice_type": "group_increase",
                  "group_id": group_id, "user_id": user_id, "self_id": self_id})


def make_gate(token="secret", api_base=base, **extra):
    g = JoinGate.__new__(JoinGate)
    g.cfg = {"enabled": True, "group_ids": "1", "api_base": api_base + "/", "api_token": token, **extra}
    g._surnames = {}
    g.logs = []
    g.logger = type("L", (), {k: (lambda s, m: g.logs.append(m)) for k in ("info", "error", "warning")})()
    return g


async def main():
    g = make_gate()

    # 判定
    ok, reason, surname, why = await g._decide("问题：手机号\n答案：13800000001")
    assert (ok, reason, surname) == (True, "", "张"), (ok, reason, surname)
    assert FakeAPI.calls[-1] == ("/api/bot/member-check", "Bearer secret", {"phone": "13800000001"})
    assert "138****0001" in why and "13800000001" not in why

    ok, reason, _, _ = await g._decide("答案：13900000000")
    assert ok is False and "还没有在平台登录过" in reason

    n = len(FakeAPI.calls)
    ok, reason, _, _ = await g._decide("答案：我是学生")
    assert ok is False and reason == mod.PHONE_MISSING
    assert len(FakeAPI.calls) == n, "没有手机号就不调接口"

    # 接口出错一律留给人工
    FakeAPI.fail = True
    assert (await g._decide("13800000001"))[0] is None
    FakeAPI.fail = False
    assert (await make_gate(token="wrong")._decide("13800000001"))[0] is None, "401"
    assert (await make_gate(token="")._decide("13800000001"))[0] is None, "没配密钥"
    assert (await make_gate(api_base="http://127.0.0.1:1")._decide("13800000001"))[0] is None, "连不上"

    # 申请：同意 / 拒绝带原因 / 一个号码多个 QQ 都放行 / 非目标群不管
    ev = request(10001, "问题：学习通手机号\n答案：13800000001")
    await g.on_group_request(ev)
    assert ev.bot.calls == [("set_group_add_request", {"flag": "f10001", "sub_type": "add", "approve": True})] and ev.stopped

    ev = request(10002, "答案：13800000001")
    await g.on_group_request(ev)
    assert ev.bot.calls[-1][1]["approve"] is True, "同一个号码换 QQ 也放行"

    ev = request(10003, "答案：13900000000")
    await g.on_group_request(ev)
    assert ev.bot.calls[-1][1]["approve"] is False and "还没有在平台登录过" in ev.bot.calls[-1][1]["reason"]
    assert not any("13900000000" in m for m in g.logs), "日志里不能出现完整手机号"

    ev = request(10004, "答案：13800000001", group_id=2)
    await g.on_group_request(ev)
    assert not ev.bot.calls and not ev.stopped

    FakeAPI.fail = True
    ev = request(10005, "答案：13800000001")
    await g.on_group_request(ev)
    assert not ev.bot.calls and not ev.stopped, "接口出错时不提交审批"
    FakeAPI.fail = False

    # 欢迎：用审批时拿到的姓氏
    ev = joined(10001)
    await g.on_group_increase(ev)
    action, kw = ev.bot.calls[-1]
    assert action == "send_group_msg" and kw["group_id"] == 1
    assert kw["message"][0] == {"type": "at", "data": {"qq": "10001"}}
    assert kw["message"][1]["data"]["text"] == " 欢迎张同学进群，感谢您的支持。", kw

    ev = request(10006, "13800000002")  # 名字不是汉字开头
    await g.on_group_request(ev)
    ev = joined(10006)
    await g.on_group_increase(ev)
    assert ev.bot.calls[-1][1]["message"][1]["data"]["text"] == " 欢迎新同学进群，感谢您的支持。"

    ev = joined(10007)  # 管理员手动拉进来的，没有姓氏
    await g.on_group_increase(ev)
    assert "欢迎新同学进群" in ev.bot.calls[-1][1]["message"][1]["data"]["text"]

    ev = joined(999)  # 机器人自己
    await g.on_group_increase(ev)
    assert not ev.bot.calls

    ev = joined(10001, group_id=2)
    await g.on_group_increase(ev)
    assert not ev.bot.calls

    g2 = make_gate(welcome_template="热烈欢迎{surname}同学！")
    g2._surnames[("1", "10008")] = ("欧阳", __import__("time").time())
    ev = joined(10008)
    await g2.on_group_increase(ev)
    assert ev.bot.calls[-1][1]["message"][1]["data"]["text"] == " 热烈欢迎欧阳同学！"

    g3 = make_gate(welcome_enabled=False)
    ev = joined(10001)
    await g3.on_group_increase(ev)
    assert not ev.bot.calls

    print("ALL PASSED")


asyncio.run(main())
server.shutdown()
