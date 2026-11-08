"""手机号校验自测：手机号提取 + phone 模式判定 + 真实 HTTP 调用（本地假接口）。"""
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
sys.modules[SPEC.name] = mod  # dataclass 需要能找回模块命名空间
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
assert JoinGate._clean_comment("问题：x\n答案：13800000001") == "问题：x ⏎ 答案：138****0001"
assert JoinGate._clean_comment("答案：1433223") == "答案：1433223", "非手机号不打码"


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

    def _handle(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        if FakeAPI.fail:
            self._reply(500, {"error": "boom"})
            return
        if self.headers.get("Authorization") != "Bearer secret":
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path == "/api/bot/member-binding":
            self._reply(200, {"ok": True, "removed": True, "qq": "10001"})
            return
        phone = body["phone"]
        if phone == "13800000001":
            self._reply(200, {"ok": True, "code": "ok", "message": "手机号已确认"})
        else:
            self._reply(200, {"ok": False, "code": "not_found", "message": "这个手机号还没有在平台登录过，请先登录后再申请"})

    do_POST = _handle
    do_DELETE = _handle

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), FakeAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"


def make_gate(token="secret", on_error="ignore", api_base=base):
    g = JoinGate.__new__(JoinGate)
    g.cfg = {
        "enabled": True,
        "xxt": {"api_base": api_base + "/", "api_token": token, "on_api_error": on_error},
        "rules": [{"group_id": "1", "match_mode": "phone", "on_empty_reason": "reject"}],
    }
    return g


async def main():
    g = make_gate()
    rule = g._rules()["1"]
    assert rule["match_mode"] == "phone"

    # 干跑（/joingate test）不调用接口
    FakeAPI.calls.clear()
    assert g._decide(rule, "答案：13800000001")[0] is True
    assert g._decide(rule, "答案：随便")[0] is False
    assert not FakeAPI.calls

    ok, text, why = await g._decide_phone(rule, "问题：手机号\n答案：13800000001", "10001", "1")
    assert ok is True and text == "", (ok, text, why)
    method, path, auth, body = FakeAPI.calls[-1]
    assert (method, path, auth) == ("POST", "/api/bot/member-check", "Bearer secret")
    assert body == {"phone": "13800000001", "qq": "10001", "group_id": "1"}
    assert "138****0001" in why and "13800000001" not in why

    ok, text, _ = await g._decide_phone(rule, "答案：13900000000", "10002", "1")
    assert ok is False and "还没有在平台登录过" in text

    n = len(FakeAPI.calls)
    ok, text, _ = await g._decide_phone(rule, "答案：我是学生", "10002", "1")
    assert ok is False and text == mod.PHONE_MISSING
    assert len(FakeAPI.calls) == n, "没有手机号就不调接口"

    # 接口出错：按配置处理
    FakeAPI.fail = True
    assert (await make_gate(on_error="ignore")._decide_phone(rule, "13800000001", "1", "1"))[0] is None
    assert (await make_gate(on_error="reject")._decide_phone(rule, "13800000001", "1", "1"))[:2] == (False, mod.API_BUSY)
    assert (await make_gate(on_error="approve")._decide_phone(rule, "13800000001", "1", "1"))[0] is True
    FakeAPI.fail = False
    assert (await make_gate(token="wrong")._decide_phone(rule, "13800000001", "1", "1"))[0] is None, "401 也算出错"
    assert (await make_gate(token="")._decide_phone(rule, "13800000001", "1", "1"))[0] is None, "没配密钥"
    assert (await make_gate(api_base="http://127.0.0.1:1")._decide_phone(rule, "13800000001", "1", "1"))[0] is None, "连不上"

    # 解绑：手机号走 phone，QQ 走 qq
    await g._unbind("13800000001")
    assert FakeAPI.calls[-1][:2] == ("DELETE", "/api/bot/member-binding") and FakeAPI.calls[-1][3] == {"phone": "13800000001"}
    await g._unbind("10001")
    assert FakeAPI.calls[-1][3] == {"qq": "10001"}

    # 入群申请全流程：phone 模式拒绝时把接口的提示回给申请人
    class Bot:
        def __init__(self):
            self.calls = []

        async def call_action(self, action, **kw):
            self.calls.append((action, kw))

    class Event:
        def __init__(self, comment):
            self.message_obj = type("M", (), {"raw_message": {
                "post_type": "request", "request_type": "group", "sub_type": "add",
                "group_id": 1, "user_id": 10002, "flag": "f1", "comment": comment,
            }})()
            self.bot = Bot()
            self.stopped = False

        def stop_event(self):
            self.stopped = True

    logs = []
    g.logger = type("L", (), {"info": lambda s, m: logs.append(m), "error": lambda s, m: logs.append(m), "warning": lambda s, m: logs.append(m)})()

    async def no_blacklist(uid):
        return False

    g._is_blacklisted = no_blacklist
    ev = Event("问题：学习通手机号\n答案：13900000000")
    await g.on_group_request(ev)
    action, kw = ev.bot.calls[-1]
    assert action == "set_group_add_request" and kw["approve"] is False
    assert "还没有在平台登录过" in kw["reason"]
    assert not any("13900000000" in m for m in logs), "日志里不能出现完整手机号"

    ev = Event("答案：13800000001")
    await g.on_group_request(ev)
    assert ev.bot.calls[-1][1]["approve"] is True and ev.stopped

    FakeAPI.fail = True
    ev = Event("答案：13800000001")
    await g.on_group_request(ev)
    assert not ev.bot.calls and not ev.stopped, "接口出错时留给人工，不提交审批"
    FakeAPI.fail = False

    print("ALL PASSED")


asyncio.run(main())
server.shutdown()
