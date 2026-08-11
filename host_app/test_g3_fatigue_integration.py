"""
G3-01B 正式接入测试 —— FatigueStateMachine 四状态串口发布验证

覆盖:
  FS-01  FatigueStateMachine 产生四类 FatigueResult
  FS-02  FatigueResult 通过 SerialPublisher.publish() 正确发布
  FS-03  无检测时状态机超时转 UNKNOWN
  FS-04  推理异常后发布 UNKNOWN
  FS-05  状态机重置后回到 UNKNOWN 初始状态

工作目录: 基于YOLOv8的疲劳驾驶检测系统/
运行: python test_g3_fatigue_integration.py
"""

import sys
import time
sys.path.insert(0, ".")

from dataclasses import dataclass
from fatigue_state import (
    FatigueStateMachine, FatigueResult, FatigueState,
    Detection, load_fatigue_config,
)
from serial_bridge import SerialPublisher, encode_dms1_frame

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


# ======================================================================
# FS-01: FatigueStateMachine 产生四类 FatigueResult
# ======================================================================
print("=== FS-01 四类状态输出 ===")

sm = FatigueStateMachine()

# 初始状态应为 UNKNOWN
r0 = sm.update([])
check("FS-01a 初始 UNKNOWN", r0.state == FatigueState.UNKNOWN,
      f"got {r0.state}")
check("FS-01b evidence_score=0", r0.evidence_score == 0)

# 模拟可靠睁眼 -> 应转为 NORMAL
clock_start = time.monotonic()
def fake_clock():
    return clock_start + 0.1  # 始终返回固定偏移

sm2 = FatigueStateMachine(clock=fake_clock)
# 注入 open_eye 检测
det_open_eye = [Detection(class_id=2, class_name="open_eye", confidence=0.85)]
r = sm2.update(det_open_eye)
check("FS-01c 睁眼 -> NORMAL", r.state == FatigueState.NORMAL,
      f"got {r.state}")
check("FS-01d NORMAL evidence_score", r.evidence_score == 85,
      f"got {r.evidence_score}")

# FATIGUE 需要连续闭眼 1.5s —— 用可编程时钟模拟
class _StepClock:
    def __init__(self, start=0.0):
        self.t = start
    def advance(self, dt):
        self.t += dt
    def __call__(self):
        return self.t

clk = _StepClock(0.0)
sm3 = FatigueStateMachine(clock=clk)

# 注入 2 秒连续闭眼
for _ in range(20):
    clk.advance(0.1)
    det_closed = [Detection(class_id=0, class_name="closed_eye", confidence=0.92)]
    r = sm3.update(det_closed)

check("FS-01e 连续闭眼2s -> FATIGUE", r.state == FatigueState.FATIGUE,
      f"got {r.state}")
check("FS-01f FATIGUE evidence_score", r.evidence_score == 92)

# YAWN 需要连续张嘴 1.0s，且眼部证据不能超时（否则 UNKNOWN 优先）
clk2 = _StepClock(0.0)
sm4 = FatigueStateMachine(clock=clk2)

# 同时注入睁眼+张嘴，保持眼部证据存活
for _ in range(15):
    clk2.advance(0.1)
    det_both = [
        Detection(class_id=2, class_name="open_eye", confidence=0.80),
        Detection(class_id=3, class_name="open_mouth", confidence=0.88),
    ]
    r = sm4.update(det_both)

check("FS-01g 睁眼+张嘴1.5s -> YAWN", r.state == FatigueState.YAWN,
      f"got {r.state}")
check("FS-01h YAWN evidence_score", r.evidence_score == 88)


# ======================================================================
# FS-02: FatigueResult 通过 SerialPublisher.publish() 发布
# ======================================================================
print("\n=== FS-02 SerialPublisher 接收 FatigueResult ===")

pub = SerialPublisher(port="TEST", log_cb=None)

# 发布 NORMAL
pub.publish(FatigueResult(
    state=FatigueState.NORMAL, evidence_score=95,
    duration_ms=1000, timestamp_ms=5000000,
))
state, conf = pub._current_state(5000.1)
check("FS-02a publish NORMAL", state == "NORMAL" and conf == 95,
      f"{state},{conf}")

# 发布 FATIGUE
pub.publish(FatigueResult(
    state=FatigueState.FATIGUE, evidence_score=91,
    duration_ms=2000, timestamp_ms=5001000,
))
state, conf = pub._current_state(5001.5)
check("FS-02b publish FATIGUE", state == "FATIGUE" and conf == 91)

# 发布 YAWN
pub.publish(FatigueResult(
    state=FatigueState.YAWN, evidence_score=88,
    duration_ms=1500, timestamp_ms=5002000,
))
state, conf = pub._current_state(5003.0)
check("FS-02c publish YAWN", state == "YAWN" and conf == 88)

# 发布 UNKNOWN
pub.publish(FatigueResult(
    state=FatigueState.UNKNOWN, evidence_score=0,
    duration_ms=0, timestamp_ms=5003000,
))
state, conf = pub._current_state(5004.0)
check("FS-02d publish UNKNOWN", state == "UNKNOWN" and conf == 0)

# 验证编码函数与 FatigueResult 联用
for st in [FatigueState.NORMAL, FatigueState.YAWN,
           FatigueState.FATIGUE, FatigueState.UNKNOWN]:
    frame = encode_dms1_frame(0, st.value, 50)
    check(f"FS-02e encode {st.value}", frame == f"DMS1,0,{st.value},50\r\n".encode())


# ======================================================================
# FS-03: 无检测时状态机超时转 UNKNOWN
# ======================================================================
print("\n=== FS-03 无检测超时 UNKNOWN ===")

clk3 = _StepClock(0.0)
sm5 = FatigueStateMachine(clock=clk3)

# 先注入睁眼进入 NORMAL
r = sm5.update([Detection(class_id=2, class_name="open_eye", confidence=0.80)])
check("FS-03a 进入 NORMAL", r.state == FatigueState.NORMAL)

# 然后停止注入, 等 1.1 秒 (超过 unknown_timeout=1.0s)
for _ in range(12):
    clk3.advance(0.1)
    r = sm5.update([])

check("FS-03b 超时 UNKNOWN", r.state == FatigueState.UNKNOWN,
      f"got {r.state} after {clk3.t}s")


# ======================================================================
# FS-04: 推理异常后发布 UNKNOWN
# ======================================================================
print("\n=== FS-04 异常路径 UNKNOWN ===")

pub2 = SerialPublisher(port="TEST", log_cb=None)
# 模拟异常处理: 直接发布 UNKNOWN
err_result = FatigueResult(
    state=FatigueState.UNKNOWN, evidence_score=0,
    duration_ms=0, timestamp_ms=6000000,
)
pub2.publish(err_result)
state, conf = pub2._current_state(6000.1)
check("FS-04a 异常后 UNKNOWN", state == "UNKNOWN" and conf == 0)

# 确认 publish() 拒绝无效 FatigueState
class _BadResult:
    state = "INVALID"
    evidence_score = 50
    timestamp_ms = 7000000

pub3 = SerialPublisher(port="TEST", log_cb=None)
pub3.publish(_BadResult())
# 应该不更新 has_result
check("FS-04b 非法 state 被忽略", not pub3._has_result)


# ======================================================================
# FS-05: 状态机重置
# ======================================================================
print("\n=== FS-05 状态机重置 ===")

clk5 = _StepClock(0.0)
sm6 = FatigueStateMachine(clock=clk5)
r = sm6.update([])
check("FS-05a 新状态机 UNKNOWN", r.state == FatigueState.UNKNOWN)

# 进入 NORMAL
clk5.advance(0.1)
r = sm6.update([Detection(class_id=2, class_name="open_eye", confidence=0.80)])
check("FS-05b NORMAL", r.state == FatigueState.NORMAL)

# 重置: 创建新实例
sm6 = FatigueStateMachine(clock=clk5)
r = sm6.update([])
check("FS-05c 重置后 UNKNOWN", r.state == FatigueState.UNKNOWN)


# ======================================================================
print(f"\n{'='*50}")
print(f"Total: {PASSED} PASS, {FAILED} FAIL")
if FAILED == 0:
    print("ALL FATIGUE INTEGRATION TESTS PASSED")
    sys.exit(0)
else:
    print(f"{FAILED} FAILURE(S)")
    sys.exit(1)
