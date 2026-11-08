"""纯逻辑自测：不依赖平台，直接跑 _decide。"""
import importlib.util
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("jg", _PLUGIN)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod  # dataclass 需要能找回模块命名空间
SPEC.loader.exec_module(mod)

JoinGate = mod.JoinGate

RULES = [
    {
        # 真实场景：群里有入群问题，comment = "问题：你好\n答案：1433223"
        "group_id": "2163040959",
        "allow_reasons": "1433223",
        "match_mode": "exact",
        "reject_reason": "申请理由不正确",
        "on_empty_reason": "reject",
    },
    {
        "group_id": "123456789",
        "allow_reasons": "我认识群主\n从B站来的\n# 这行是注释\n",
        "match_mode": "exact",
        "reject_reason": "理由不对",
        "on_empty_reason": "reject",
    },
    {
        "group_id": "987654321",
        "allow_reasons": "答案：晴天",
        "match_mode": "contains",
        "on_empty_reason": "approve",
    },
]

g = JoinGate.__new__(JoinGate)  # 跳过 __init__，只测纯逻辑
g.cfg = {"enabled": True, "rules": RULES}
rules = g._rules()

assert set(rules) == {"2163040959", "123456789", "987654321"}, rules
assert rules["123456789"]["reasons"] == ["我认识群主", "从B站来的"], rules["123456789"]
assert rules["123456789"]["match_mode"] == "exact"
assert rules["987654321"]["on_empty_reason"] == "approve"

cases = [
    # ---- 真实场景：问题/答案包装 + exact ----
    ("2163040959", "问题：你好\n答案：1433223", True),
    ("2163040959", "问题：你好\n答案： 1433223 ", True),
    ("2163040959", "答案：1433223", True),
    ("2163040959", "1433223", True),
    ("2163040959", "问题：你好\n答案：14332230", False),  # 不能被当成包含匹配
    ("2163040959", "问题：你好\n答案：123", False),
    ("2163040959", "", False),
    # ---- 普通 exact ----
    ("123456789", "我认识群主", True),
    ("123456789", "  我认识群主  ", True),
    ("123456789", "从B站来的", True),
    ("123456789", "我认识群主啊", False),
    ("123456789", "", False),
    # ---- contains ----
    ("987654321", "", True),                     # 空理由 -> approve
    ("987654321", "补充一下答案：晴天", True),
    ("987654321", "我的答案是：晴天哦", False),
    ("987654321", "随便写的", False),
    # ---- 非目标群 ----
    ("111111111", "1433223", None),
]

failed = 0
for gid, comment, expect in cases:
    rule = rules.get(gid)
    if expect is None:
        if rule is not None:
            print(f"FAIL group={gid} 不应有规则")
            failed += 1
        continue
    got, why = g._decide(rule, comment)
    flag = "OK  " if got is expect else "FAIL"
    if got is not expect:
        failed += 1
    shown = comment.replace("\n", "⏎")
    print(f"{flag} group={gid} comment={shown!r} -> {got} ({why})")

print("\nALL PASSED" if failed == 0 else f"\n{failed} FAILED")
raise SystemExit(1 if failed else 0)
