"""进群验证逻辑自测：用假 bot + 内存 KV，不依赖真实平台。"""
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("jg", _PLUGIN)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod  # dataclass 需要能找回模块命名空间
SPEC.loader.exec_module(mod)

JoinGate = mod.JoinGate
Challenge = mod.Challenge


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {}


def make_gate(max_failures=2, enabled=True):
    g = JoinGate.__new__(JoinGate)
    g.cfg = {
        "enabled": True,
        "verify": {
            "enabled": enabled,
            "timeout_seconds": 120,
            "operand_max": 20,
            "max_failures": max_failures,
            "blacklist_on_max": True,
            "kick_on_wrong": True,
        },
        "rules": [
            {
                "__template_key": "rule",
                "group_id": "2163040959",
                "allow_reasons": "1433223",
                "match_mode": "exact",
                "verify_new_member": True,
            }
        ],
    }
    g._pending = {}
    g._mem = {}
    # 内存 KV 替换真实 DB
    async def get_kv(key, default=None):
        return g._mem.get(key, default)

    async def put_kv(key, value):
        g._mem[key] = value

    g.get_kv_data = get_kv
    g.put_kv_data = put_kv
    g.logger = type("L", (), {"info": lambda *a, **k: None,
                              "error": lambda *a, **k: None,
                              "warning": lambda *a, **k: None})()
    return g


def kicks(bot):
    return [kw for a, kw in bot.calls if a == "set_group_kick"]


def sends(bot):
    return [kw for a, kw in bot.calls if a == "send_group_msg"]


async def main():
    # ---- 1) 第一次答错：踢出但没拉黑 ----
    g = make_gate()
    bot = FakeBot()
    await g._fail(bot, "2163040959", "10001", "回答错误（应回答 12）")
    assert len(kicks(bot)) == 1, bot.calls
    assert kicks(bot)[0]["reject_add_request"] is False, "第一次不应拉黑"
    assert (await g._failures())["10001"] == 1
    assert await g._is_blacklisted("10001") is False
    print("OK  第 1 次失败 -> 踢出，未拉黑")

    # ---- 2) 第二次失败：拉黑 + 拒绝再加群 ----
    await g._fail(bot, "2163040959", "10001", "超时未回答")
    assert len(kicks(bot)) == 2
    assert kicks(bot)[1]["reject_add_request"] is True, "第二次应拉黑"
    assert (await g._failures())["10001"] == 2
    assert await g._is_blacklisted("10001") is True
    print("OK  第 2 次失败 -> 踢出 + 拉黑（reject_add_request=True）")

    # ---- 3) 黑名单查询 / 解除 / 重置 ----
    assert await g._remove_blacklist("10001") is True
    assert await g._is_blacklisted("10001") is False
    print("OK  unban 生效")

    # ---- 4) 答案归一化 ----
    norm = mod._normalized_answers
    assert "12" in norm("12")
    assert "12" in norm(" 1 2 ")
    assert "12" in norm("12。")
    assert "12" in norm("=12")
    assert "12" not in norm("13")
    print("OK  答案归一化（空格/句号/等号）")

    # ---- 5) 别名：max_failures 可配置为 3 ----
    g3 = make_gate(max_failures=3)
    b3 = FakeBot()
    for _ in range(2):
        await g3._fail(b3, "2163040959", "20002", "错")
    assert await g3._is_blacklisted("20002") is False, "3 次上限时第 2 次不应拉黑"
    await g3._fail(b3, "2163040959", "20002", "错")
    assert await g3._is_blacklisted("20002") is True
    print("OK  max_failures=3 时第 3 次才拉黑")

    # ---- 6) 超时守卫会触发失败处理 ----
    g6 = make_gate()
    b6 = FakeBot()
    g6._find_bot = lambda: b6
    ch = Challenge(
        group_id="2163040959", user_id="30003", a=3, b=4, answer=7,
        prompt="x", deadline=time.time() - 1,
    )
    g6._pending[("2163040959", "30003")] = ch
    await g6._timeout_watch(ch)
    assert await g6._is_blacklisted("30003") is False
    assert (await g6._failures())["30003"] == 1
    assert len(kicks(b6)) == 1
    assert ("2163040959", "30003") not in g6._pending
    print("OK  超时 -> 记失败 + 踢出 + 清理 pending")

    print("\nALL PASSED")


asyncio.run(main())
