"""
G3-01 A4 独立验证: SerialPublisher + FatigueStateMachine 集成桥梁

验证:
- FatigueResult 通过 publish() 正确流入 SerialPublisher
- 状态变更后 _current_state() 返回正确 DMS1 状态
- 发布器对非法/越界/边界 FatigueResult 的防御行为
- Fatiguestate 枚举 → encode_dms1_frame 的完整映射
"""
import sys
import os
import time

WORKDIR = os.path.join(os.path.dirname(__file__), "..", "..",
                       "host_app")
sys.path.insert(0, WORKDIR)

from dataclasses import dataclass

from fatigue_state import (
    FatigueStateMachine, FatigueResult, FatigueState,
    Detection, load_fatigue_config,
)
from serial_bridge import (
    SerialPublisher, encode_dms1_frame, parse_dms1_frame,
    VALID_STATES, STALE_TIMEOUT,
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  -- {detail}")


# ---------------------------------------------------------------------------
# A4-FB-01: FatigueState 枚举 → DMS1 字符串映射
# ---------------------------------------------------------------------------
print("=== A4-FB-01 枚举映射 ===")

expected_map = {
    FatigueState.NORMAL: "NORMAL",
    FatigueState.YAWN: "YAWN",
    FatigueState.FATIGUE: "FATIGUE",
    FatigueState.UNKNOWN: "UNKNOWN",
}

for enum_val, str_val in expected_map.items():
    check(f"A4-FB-01a {enum_val.name} → {str_val}",
          enum_val.value == str_val)
    check(f"A4-FB-01b {str_val} in VALID_STATES",
          str_val in VALID_STATES)

# 验证无遗漏
check("A4-FB-01c exactly 4 states", len(FatigueState) == 4)
check("A4-FB-01d VALID_STATES matches", VALID_STATES == {"NORMAL", "YAWN", "FATIGUE", "UNKNOWN"})

# ---------------------------------------------------------------------------
# A4-FB-02: FatigueResult 通过 publish() 传入 SerialPublisher
# ---------------------------------------------------------------------------
print("\n=== A4-FB-02 publish 集成 ===")

pub = SerialPublisher(port="TEST", log_cb=None)

test_cases = [
    (FatigueState.NORMAL, 85, 1000, 1000000, 1000.5, "NORMAL", 85),
    (FatigueState.FATIGUE, 91, 2000, 2000000, 2000.3, "FATIGUE", 91),
    (FatigueState.YAWN, 88, 1500, 3000000, 3000.1, "YAWN", 88),
    (FatigueState.UNKNOWN, 0, 0, 4000000, 4001.0, "UNKNOWN", 0),
]

for enum_val, score, dur, ts, now, exp_state, exp_conf in test_cases:
    pub.publish(FatigueResult(
        state=enum_val, evidence_score=score,
        duration_ms=dur, timestamp_ms=ts,
    ))
    state, conf = pub._current_state(now)
    check(f"A4-FB-02 {enum_val.name} state", state == exp_state,
          f"expected {exp_state}, got {state}")
    check(f"A4-FB-02 {enum_val.name} conf", conf == exp_conf,
          f"expected {exp_conf}, got {conf}")

# ---------------------------------------------------------------------------
# A4-FB-03: publish 后立即 encode → 完整 DMS1 帧验证
# ---------------------------------------------------------------------------
print("\n=== A4-FB-03 publish → encode 完整帧 ===")

expected_frames = [
    (FatigueState.NORMAL, 85, "DMS1,0,NORMAL,85\r\n"),
    (FatigueState.YAWN, 88, "DMS1,0,YAWN,88\r\n"),
    (FatigueState.FATIGUE, 91, "DMS1,0,FATIGUE,91\r\n"),
    (FatigueState.UNKNOWN, 0, "DMS1,0,UNKNOWN,0\r\n"),
]

for enum_val, score, expected_raw in expected_frames:
    frame = encode_dms1_frame(0, enum_val.value, score)
    check(f"A4-FB-03a {enum_val.name} frame exact",
          frame == expected_raw.encode("ascii"),
          f"got {frame!r}")

    # 往返验证
    parsed = parse_dms1_frame(frame)
    check(f"A4-FB-03b {enum_val.name} round-trip",
          parsed is not None and
          parsed["state"] == enum_val.value and
          parsed["confidence"] == score)

# ---------------------------------------------------------------------------
# A4-FB-04: 状态机 NORMAL → FATIGUE 完整流程 ↔ publish
# ---------------------------------------------------------------------------
print("\n=== A4-FB-04 状态机 → publish 流程 ===")

class StepClock:
    def __init__(self, start=0.0):
        self.t = start
    def advance(self, dt):
        self.t += dt
    def __call__(self):
        return self.t

clk = StepClock(0.0)
sm = FatigueStateMachine(clock=clk)
pub2 = SerialPublisher(port="TEST", clock=clk, log_cb=None)

# 阶段 1: 初始 → UNKNOWN
r = sm.update([])
check("A4-FB-04a sm initial UNKNOWN", r.state == FatigueState.UNKNOWN)
pub2.publish(r)
s, c = pub2._current_state(clk())
check("A4-FB-04b pub initial UNKNOWN", s == "UNKNOWN" and c == 0)

# 阶段 2: 睁眼 → NORMAL
clk.advance(0.1)
r = sm.update([Detection(class_id=2, class_name="open_eye", confidence=0.80)])
check("A4-FB-04c sm NORMAL", r.state == FatigueState.NORMAL)
pub2.publish(r)
s, c = pub2._current_state(clk())
check("A4-FB-04d pub NORMAL", s == "NORMAL" and c == 80)

# 阶段 3: 连续闭眼 2s → FATIGUE(FatigueStateMachine: closed_eye_trigger=1.5s)
for _ in range(25):
    clk.advance(0.1)
    r = sm.update([Detection(class_id=0, class_name="closed_eye", confidence=0.92)])
pub2.publish(r)
check("A4-FB-04e sm FATIGUE", r.state == FatigueState.FATIGUE)
s, c = pub2._current_state(clk())
check("A4-FB-04f pub FATIGUE", s == "FATIGUE" and c == 92)

# 阶段 4: 无检测超时 → UNKNOWN
for _ in range(20):
    clk.advance(0.1)
    r = sm.update([])
pub2.publish(r)
check("A4-FB-04g sm timeout UNKNOWN", r.state == FatigueState.UNKNOWN)
s, c = pub2._current_state(clk())
check("A4-FB-04h pub timeout UNKNOWN", s == "UNKNOWN" and c == 0)

# ---------------------------------------------------------------------------
# A4-FB-05: 陈旧超时 — publish 后不更新导致 UNKNOWN
# ---------------------------------------------------------------------------
print("\n=== A4-FB-05 陈旧超时 ===")

clk5 = StepClock(5000.0)
pub5 = SerialPublisher(port="TEST", clock=clk5, log_cb=None)

# 发布 NORMAL
pub5.publish(FatigueResult(
    state=FatigueState.NORMAL, evidence_score=90,
    duration_ms=500, timestamp_ms=int(clk5.t * 1000),
))
s, c = pub5._current_state(clk5())
check("A4-FB-05a initial NORMAL", s == "NORMAL")

# 推进 1.49s — 仍 NORMAL
clk5.advance(1.49)
s, c = pub5._current_state(clk5())
check("A4-FB-05b 1.49s still NORMAL", s == "NORMAL",
      f"got {s} at t={clk5.t}")

# 推进到 1.50s — 应 UNKNOWN
clk5.advance(0.02)  # 累计 1.51
s, c = pub5._current_state(clk5())
check("A4-FB-05c 1.51s UNKNOWN", s == "UNKNOWN" and c == 0,
      f"got {s},{c} at t={clk5.t}")

# ---------------------------------------------------------------------------
# A4-FB-06: publish() 防御 — 无 state 属性、枚举 value 兼容
# ---------------------------------------------------------------------------
print("\n=== A4-FB-06 publish 防御 ===")

pub6 = SerialPublisher(port="TEST", log_cb=None)

# 无 state 属性
class _NoState:
    evidence_score = 50
    timestamp_ms = 6000000

pub6.publish(_NoState())
# 应不更新 has_result
check("A4-FB-06a no state attr ignored", not pub6._has_result)

# 枚举 .value 兼容
class _EnumLike:
    class state:
        value = "FATIGUE"
    evidence_score = 60
    timestamp_ms = 7000000

pub6.publish(_EnumLike())
check("A4-FB-06b enum value compat", pub6._has_result)
check("A4-FB-06c enum value state", pub6._latest_state == "FATIGUE")

# 时间戳为 0
pub6.publish(FatigueResult(
    state=FatigueState.NORMAL, evidence_score=70,
    duration_ms=0, timestamp_ms=0,
))
s, c = pub6._current_state(7000.0)
check("A4-FB-06d timestamp 0", s == "UNKNOWN" and c == 0,
      f"got {s},{c} — ts=0 makes age=7000s, should be stale")

# ---------------------------------------------------------------------------
# A4-FB-07: 疲劳恢复 — UNKNOWN 后新结果恢复
# ---------------------------------------------------------------------------
print("\n=== A4-FB-07 恢复后发布 ===")

clk7 = StepClock(8000.0)
pub7 = SerialPublisher(port="TEST", clock=clk7, log_cb=None)

pub7.publish(FatigueResult(
    state=FatigueState.NORMAL, evidence_score=75,
    duration_ms=200, timestamp_ms=int(clk7.t * 1000),
))
s, c = pub7._current_state(clk7())
check("A4-FB-07a publish NORMAL", s == "NORMAL" and c == 75)

# 超时 → UNKNOWN
clk7.advance(STALE_TIMEOUT + 0.1)
s, c = pub7._current_state(clk7())
check("A4-FB-07b stale UNKNOWN", s == "UNKNOWN")

# 新结果到达 → 恢复
pub7.publish(FatigueResult(
    state=FatigueState.YAWN, evidence_score=82,
    duration_ms=300, timestamp_ms=int(clk7.t * 1000),
))
s, c = pub7._current_state(clk7())
check("A4-FB-07c recovered YAWN", s == "YAWN" and c == 82)

# ---------------------------------------------------------------------------
# A4-FB-08: 配置加载后的状态机也可以用 publish 集成
# ---------------------------------------------------------------------------
print("\n=== A4-FB-08 配置状态机集成 ===")

cfg = load_fatigue_config()
check("A4-FB-08a config loaded", isinstance(cfg, dict) and len(cfg) > 0)
check("A4-FB-08b closed_eye_trigger", "closed_eye_trigger" in cfg)
check("A4-FB-08c unknown_timeout", "unknown_timeout" in cfg)

sm8 = FatigueStateMachine(config=cfg)
pub8 = SerialPublisher(port="TEST", log_cb=None)

r = sm8.update([])
pub8.publish(r)
check("A4-FB-08d config sm → publish", r.state == FatigueState.UNKNOWN)

# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"A4 Fatigue Bridge Tests: {PASSED} PASS, {FAILED} FAIL")
if FAILED:
    print(f"{FAILED} FAILURE(S)")
    sys.exit(1)
else:
    print("ALL A4 FATIGUE BRIDGE TESTS PASSED")
