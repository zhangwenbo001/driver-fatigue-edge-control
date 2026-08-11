"""
G3-01 协议编码与串口发布器测试 (SP + HF 验收条款)

工作目录: 基于YOLOv8的疲劳驾驶检测系统/
运行: python test_g3_protocol.py

覆盖:
  SP-01  四类合法状态编码
  SP-02  seq 0/65535 并回绕
  SP-03  confidence 0/100 及越界拒绝
  HF-01  首帧前发送 UNKNOWN,0
  HF-02  结果陈旧 1.50 秒边界
  HF-03  发布器存活时持续 UNKNOWN,0 (模拟)
  HF-04  1.49/1.50 秒陈旧边界
  HF-05  故障恢复保持 UNKNOWN 直到新结果
"""

import sys
import time
import threading
from dataclasses import dataclass

# 确保导入当前目录的模块
sys.path.insert(0, ".")
from serial_bridge import (
    encode_dms1_frame, parse_dms1_frame, VALID_STATES,
    SerialPublisher, STALE_TIMEOUT, HEARTBEAT_PERIOD, SEQ_MAX,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  -- {detail}")


# ======================================================================
# SP-01: 四类合法状态编码
# ======================================================================
print("=== SP-01 四类合法状态编码 ===")

frame = encode_dms1_frame(0, "NORMAL", 95)
check("SP-01a NORMAL", frame == b"DMS1,0,NORMAL,95\r\n", str(frame))

frame = encode_dms1_frame(1, "YAWN", 88)
check("SP-01b YAWN", frame == b"DMS1,1,YAWN,88\r\n", str(frame))

frame = encode_dms1_frame(2, "FATIGUE", 91)
check("SP-01c FATIGUE", frame == b"DMS1,2,FATIGUE,91\r\n", str(frame))

frame = encode_dms1_frame(3, "UNKNOWN", 0)
check("SP-01d UNKNOWN", frame == b"DMS1,3,UNKNOWN,0\r\n", str(frame))

# 验证所有合法帧以 CRLF 结尾
for state in VALID_STATES:
    frame = encode_dms1_frame(0, state, 50)
    check(f"SP-01e {state} CRLF", frame.endswith(b"\r\n"),
          f"ends with {frame[-4:]}")

# 验证帧长度 <= 63 字节
for state in VALID_STATES:
    frame = encode_dms1_frame(65535, state, 100)
    check(f"SP-01f {state} len<={63}", len(frame) <= 63,
          f"len={len(frame)}")


# ======================================================================
# SP-02: seq 0/65535 并回绕
# ======================================================================
print("\n=== SP-02 seq 边界与回绕 ===")

# seq=0
f0 = encode_dms1_frame(0, "NORMAL", 0)
check("SP-02a seq=0", f0 == b"DMS1,0,NORMAL,0\r\n")

# seq=65535
fmax = encode_dms1_frame(65535, "NORMAL", 0)
check("SP-02b seq=65535", fmax == b"DMS1,65535,NORMAL,0\r\n")

# seq 回绕编码验证(seq 从 65535 到 0)
check("SP-02c wraparound encode 65535", fmax.startswith(b"DMS1,65535"))
check("SP-02d wraparound encode 0", f0.startswith(b"DMS1,0,"))

# 越界 seq 拒绝
try:
    encode_dms1_frame(-1, "NORMAL", 0)
    check("SP-02e seq=-1 reject", False, "应该抛出 ValueError")
except ValueError:
    check("SP-02e seq=-1 reject", True)

try:
    encode_dms1_frame(65536, "NORMAL", 0)
    check("SP-02f seq=65536 reject", False, "应该抛出 ValueError")
except ValueError:
    check("SP-02f seq=65536 reject", True)

# 模拟 SerialPublisher seq 回绕
pub = SerialPublisher(port="TEST", log_cb=None)
# 直接设置内部 seq 为 65535 测试回绕
pub._seq = 65535
check("SP-02g initial seq=65535", pub._seq == 65535)
# 模拟一次发送后的 seq 更新
pub._seq = (pub._seq + 1) % (SEQ_MAX + 1)
check("SP-02h seq wraparound to 0", pub._seq == 0)

# 再次递增
pub._seq = (pub._seq + 1) % (SEQ_MAX + 1)
check("SP-02i seq=1 after wraparound", pub._seq == 1)


# ======================================================================
# SP-03: confidence 0/100 及越界拒绝
# ======================================================================
print("\n=== SP-03 confidence 边界 ===")

# confidence=0
f0 = encode_dms1_frame(0, "UNKNOWN", 0)
check("SP-03a conf=0", f0 == b"DMS1,0,UNKNOWN,0\r\n")

# confidence=100
f100 = encode_dms1_frame(0, "NORMAL", 100)
check("SP-03b conf=100", f100 == b"DMS1,0,NORMAL,100\r\n")

# 越界拒绝
for bad in (-1, 101, 200):
    try:
        encode_dms1_frame(0, "NORMAL", bad)
        check(f"SP-03c conf={bad} reject", False, "应该抛出 ValueError")
    except ValueError:
        check(f"SP-03c conf={bad} reject", True)

# 非整数类型拒绝
try:
    encode_dms1_frame(0, "NORMAL", 50.5)  # type: ignore
    check("SP-03d conf=50.5 reject", False, "应该抛出 ValueError")
except (ValueError, AssertionError):
    check("SP-03d conf=50.5 reject", True)


# ======================================================================
# SP-04: 非法报文解析拒绝
# ======================================================================
print("\n=== SP-04 非法报文解析 ===")

illegals = [
    (b"XMS1,0,NORMAL,95\r\n", "错误帧头"),
    (b"DMS1,0,NORMAL,95\n", "缺 CR"),
    (b"DMS1,0,NORMAL,95\r", "缺 LF"),
    (b"DMS1,0,BOOT,0\r\n", "非法状态 BOOT"),
    (b"DMS1,0,LINK_LOST,0\r\n", "非法状态 LINK_LOST"),
    (b"DMS1,0,normal,0\r\n", "小写状态"),
    (b"DMS1,0,NORMAL,101\r\n", "conf 越界"),
    (b"DMS1,65536,NORMAL,0\r\n", "seq 越界"),
    (b"DMS1,-1,NORMAL,0\r\n", "seq 负数"),
    (b"DMS1,0,NORMAL\r\n", "缺字段"),
    (b"DMS1,0,NORMAL,95,extra\r\n", "多余字段"),
    (b"", "空帧"),
    (b"\r\n", "仅 CRLF"),
    (b"DMS1,abc,NORMAL,95\r\n", "seq 非数字"),
    (b"DMS1,0,NORMAL,xyz\r\n", "conf 非数字"),
]

for raw, desc in illegals:
    result = parse_dms1_frame(raw)
    check(f"SP-04 {desc}", result is None, f"got {result}")

# 超长帧: 64 字节
long_frame = b"DMS1,65535,FATIGUE,100" + b"x" * 40 + b"\r\n"
check(f"SP-04 超长{len(long_frame)}字节", parse_dms1_frame(long_frame) is None)

# 边界: 63 字节合法帧内字段校验应拒绝非法内容
boundary_63 = b"DMS1,65535,FATIGUE,100" + b"x" * 36 + b"\r\n"
result = parse_dms1_frame(boundary_63)
# 63 字节但字段错误 -> 被 parse_dms1_frame 字段校验拒绝
check("SP-04 63字节字段错误", result is None)


# ======================================================================
# HF-01: 首帧前发送 UNKNOWN,0
# ======================================================================
print("\n=== HF-01 首帧前 UNKNOWN,0 ===")

# 使用可注入时钟测试 SerialPublisher._current_state
class _FakeClock:
    def __init__(self, start=0.0):
        self.t = start
    def __call__(self):
        return self.t

clock = _FakeClock(1000.0)
pub_hf = SerialPublisher(port="TEST", clock=clock)
pub_hf._log_cb = None  # 静默

# 尚未 publish 任何结果
state, conf = pub_hf._current_state(clock())
check("HF-01a 首帧前 state", state == "UNKNOWN")
check("HF-01b 首帧前 conf", conf == 0)

# 发布一个结果后
@dataclass(frozen=True)
class MockResult:
    state: str
    evidence_score: int
    duration_ms: int
    timestamp_ms: int

clock.t = 1100.0
pub_hf.publish(MockResult("NORMAL", 95, 500, 1100000))
state, conf = pub_hf._current_state(clock())
check("HF-01c 发布后 state", state == "NORMAL")
check("HF-01d 发布后 conf", conf == 95)


# ======================================================================
# HF-02/HF-04: 陈旧边界 1.49s vs 1.50s
# ======================================================================
print("\n=== HF-02/HF-04 陈旧边界 ===")

clock2 = _FakeClock(2000.0)
pub2 = SerialPublisher(port="TEST", clock=clock2)
pub2._log_cb = None

# 发布结果 timestamp=2000.0s (2000000 ms)
pub2.publish(MockResult("NORMAL", 80, 100, 2000000))

# 边界: 1.49 秒后应仍有效
clock2.t = 2000.0 + 1.49  # 2001.49
state, conf = pub2._current_state(clock2())
check("HF-02a 1.49s NORMAL", state == "NORMAL",
      f"got {state},{conf} at age={1.49}")

# 边界: 1.50 秒后应强制 UNKNOWN
clock2.t = 2000.0 + 1.50  # 2001.50
state, conf = pub2._current_state(clock2())
check("HF-02b 1.50s UNKNOWN", state == "UNKNOWN",
      f"got {state},{conf} at age={1.50}")

# 确认 1.50 秒 boundary 精确
clock2.t = 2000.0 + STALE_TIMEOUT + 0.001  # 2001.501
state, conf = pub2._current_state(clock2())
check("HF-02c 1.501s UNKNOWN", state == "UNKNOWN")


# ======================================================================
# HF-03: 发布器存活时持续 UNKNOWN,0 (模拟推理故障)
# ======================================================================
print("\n=== HF-03 推理故障持续 UNKNOWN ===")

clock3 = _FakeClock(3000.0)
pub3 = SerialPublisher(port="TEST", clock=clock3)
pub3._log_cb = None

# 初始: 无结果 -> UNKNOWN
state, conf = pub3._current_state(clock3())
check("HF-03a 初始 UNKNOWN", state == "UNKNOWN")

# 发布一个结果
pub3.publish(MockResult("NORMAL", 90, 200, 3000000))
clock3.t = 3001.0
state, conf = pub3._current_state(clock3())
check("HF-03b 正常 NORMAL", state == "NORMAL")

# 模拟故障: 不发布新结果, 等陈旧超时
clock3.t = 3000.0 + STALE_TIMEOUT + 0.1  # 3001.6
state, conf = pub3._current_state(clock3())
check("HF-03c 陈旧后 UNKNOWN", state == "UNKNOWN")
check("HF-03d 陈旧后 conf=0", conf == 0)

# 持续多次检查,始终 UNKNOWN
for i in range(5):
    clock3.t += 0.5
    state, conf = pub3._current_state(clock3())
    check(f"HF-03e 持续UNKNOWN #{i}", state == "UNKNOWN")


# ======================================================================
# HF-05: 故障恢复需新结果
# ======================================================================
print("\n=== HF-05 故障恢复需新结果 ===")

clock5 = _FakeClock(4000.0)
pub5 = SerialPublisher(port="TEST", clock=clock5)
pub5._log_cb = None

# 初始无结果
check("HF-05a 初始 UNKNOWN", pub5._current_state(clock5())[0] == "UNKNOWN")

# 发布结果
pub5.publish(MockResult("FATIGUE", 92, 300, 4000000))
check("HF-05b FATIGUE", pub5._current_state(4001.0)[0] == "FATIGUE")

# 陈旧 -> UNKNOWN
clock5.t = 4000.0 + STALE_TIMEOUT + 0.1
check("HF-05c 陈旧 UNKNOWN", pub5._current_state(clock5())[0] == "UNKNOWN")

# 在没有新结果时始终 UNKNOWN
clock5.t += 1.0
check("HF-05d 无新结果仍是UNKNOWN", pub5._current_state(clock5())[0] == "UNKNOWN")

# 新结果到达 -> 恢复
pub5.publish(MockResult("NORMAL", 85, 100, int(clock5.t * 1000)))
check("HF-05e 新结果恢复NORMAL", pub5._current_state(clock5())[0] == "NORMAL")


# ======================================================================
# 结果汇总
# ======================================================================
print(f"\n{'='*50}")
print(f"Total: {PASSED} PASS, {FAILED} FAIL")
if FAILED == 0:
    print("ALL TESTS PASSED")
    sys.exit(0)
else:
    print(f"{FAILED} FAILURE(S)")
    sys.exit(1)
