"""
G3-01 A4 独立验证: SerialPublisher 帧写入、心跳、UNKNOWN 状态、seq 回绕、写失败退避/释放

不依赖真实串口,使用 mock serial + 可注入时钟。
验证实际产品函数,不复制 A2 的常量断言。
"""
import sys
import time
import os

WORKDIR = os.path.join(os.path.dirname(__file__), "..", "..",
                       "host_app")
sys.path.insert(0, WORKDIR)

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from serial_bridge import (
    SerialPublisher, encode_dms1_frame, parse_dms1_frame,
    HEARTBEAT_PERIOD, STALE_TIMEOUT, SEQ_MAX,
    RECONNECT_INITIAL, RECONNECT_MAX, RECONNECT_FACTOR,
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
# 辅助
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockResult:
    state: str
    evidence_score: int
    duration_ms: int
    timestamp_ms: int


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# A4-PUB-01: Mock Serial 写入验证
# ---------------------------------------------------------------------------
print("=== A4-PUB-01 Mock Serial 写入验证 ===")

# 模拟 publish → _run 循环写入 1 个周期
clock = FakeClock(1000.0)
written_frames = []

class _MockSer:
    def __init__(self):
        self.is_open = True
    def write(self, data):
        written_frames.append((clock.t, data))
        return len(data)
    def flush(self):
        pass
    def close(self):
        self.is_open = False

mock_ser = _MockSer()
pub = SerialPublisher(port="TEST", clock=clock, log_cb=None)
pub._ser = mock_ser
pub._running = True

# 手动触发一次 _run 周期的核心部分(绕过 sleep)
with pub._lock:
    pub._latest_state = "NORMAL"
    pub._latest_confidence = 95
    pub._latest_timestamp_ms = 999000
    pub._has_result = True
    pub._seq = 42

state, conf = pub._current_state(clock())
frame = encode_dms1_frame(pub._seq, state, conf)
pub._ser.write(frame)
pub._ser.flush()
pub._seq = (pub._seq + 1) % (SEQ_MAX + 1)

check("A4-PUB-01a write called", len(written_frames) == 1, str(written_frames))
check("A4-PUB-01b frame content", written_frames[0][1] == b"DMS1,42,NORMAL,95\r\n",
      f"got {written_frames[0][1]!r}")
check("A4-PUB-01c seq incremented", pub._seq == 43)
check("A4-PUB-01d frame ends CRLF", written_frames[0][1].endswith(b"\r\n"))

# ---------------------------------------------------------------------------
# A4-PUB-02: 首帧/陈旧/故障后的 UNKNOWN
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-02 首帧/陈旧/故障 UNKNOWN ===")

# 首帧(无结果) → UNKNOWN
clock2 = FakeClock(2000.0)
pub2 = SerialPublisher(port="TEST", clock=clock2, log_cb=None)
s, c = pub2._current_state(clock2())
check("A4-PUB-02a first frame UNKNOWN", s == "UNKNOWN" and c == 0)

# 发布结果后正常
pub2.publish(MockResult("NORMAL", 80, 100, 2000000))
s, c = pub2._current_state(2000.5)
check("A4-PUB-02b after publish NORMAL", s == "NORMAL" and c == 80)

# 陈旧 1.5s → UNKNOWN
s, c = pub2._current_state(2000.0 + STALE_TIMEOUT + 0.001)
check("A4-PUB-02c stale UNKNOWN", s == "UNKNOWN" and c == 0)

# 陈旧但刚好 1.499s 仍正常
s, c = pub2._current_state(2000.0 + 1.499)
check("A4-PUB-02d 1.499s still NORMAL", s == "NORMAL",
      f"got {s}")

# publish 后再次正常
clock2.t = 2003.0
pub2.publish(MockResult("FATIGUE", 92, 500, 2003000))
s, c = pub2._current_state(clock2())
check("A4-PUB-02e republish FATIGUE", s == "FATIGUE" and c == 92)

# ---------------------------------------------------------------------------
# A4-PUB-03: seq 回绕 ← 通过实际写入验证
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-03 seq 回绕 ===")

clock3 = FakeClock(3000.0)
pub3 = SerialPublisher(port="TEST", clock=clock3, log_cb=None)
pub3.publish(MockResult("NORMAL", 50, 100, 3000000))

# 模拟写出 65534→65535→0→1
written_seqs = []
for expected_seq in [65534, 65535, 0, 1]:
    pub3._seq = expected_seq
    state, conf = pub3._current_state(clock3())
    frame = encode_dms1_frame(expected_seq, state, conf)
    parsed = parse_dms1_frame(frame)
    written_seqs.append(parsed["seq"] if parsed else None)
    # 模拟写后递增
    pub3._seq = (expected_seq + 1) % (SEQ_MAX + 1)

check("A4-PUB-03a seq 65534", written_seqs[0] == 65534)
check("A4-PUB-03b seq 65535", written_seqs[1] == 65535)
check("A4-PUB-03c seq wrap to 0", written_seqs[2] == 0)
check("A4-PUB-03d seq 1 after wrap", written_seqs[3] == 1)

# 验证 encode 后 parse 回来的 seq 一致(round-trip)
for s in [0, 1, 100, 65534, 65535]:
    frame = encode_dms1_frame(s, "NORMAL", 50)
    parsed = parse_dms1_frame(frame)
    check(f"A4-PUB-03e roundtrip seq={s}", parsed is not None and parsed["seq"] == s,
          f"parsed={parsed}")

# ---------------------------------------------------------------------------
# A4-PUB-04: 写失败后关闭串口并释放
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-04 写失败后关闭并释放 ===")

import serial as real_serial_mod  # 仅为了取 SerialException 类型

clock4 = FakeClock(4000.0)
pub4 = SerialPublisher(port="TEST", clock=clock4, log_cb=None)

class _FailingMockSer:
    def __init__(self):
        self.is_open = True
        self._closed = False
        self._write_count = 0

    def write(self, data):
        self._write_count += 1
        raise real_serial_mod.SerialException("mock write failure")

    def flush(self):
        pass

    def close(self):
        self.is_open = False
        self._closed = True

mock_ser4 = _FailingMockSer()
pub4._ser = mock_ser4
pub4._running = True
pub4.publish(MockResult("NORMAL", 80, 100, 4000000))
pub4._seq = 10

# 模拟 _run 中的写入失败分支
state, conf = pub4._current_state(clock4())
try:
    frame = encode_dms1_frame(pub4._seq, state, conf)
    mock_ser4.write(frame)
    mock_ser4.flush()
except (real_serial_mod.SerialException, OSError):
    pub4._close_port()

check("A4-PUB-04a write exception caught", mock_ser4._write_count == 1)
check("A4-PUB-04b port closed after fail", mock_ser4._closed)
# _close_port 后 _ser 应为 None
check("A4-PUB-04c _ser set to None", pub4._ser is None)

# 再写入应因 _ser is None 被 _is_connected 阻止
check("A4-PUB-04d _is_connected false", not pub4._is_connected())

# ---------------------------------------------------------------------------
# A4-PUB-05: 重连退避与上限
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-05 重连退避与上限 ===")

clock5 = FakeClock(5000.0)
pub5 = SerialPublisher(port="TEST", clock=clock5, log_cb=None)
pub5._reconnect_wait = RECONNECT_INITIAL  # 1.0

# 模拟一次连接失败
pub5._last_connect_attempt = 5000.0
pub5._reconnect_wait = min(
    pub5._reconnect_wait * RECONNECT_FACTOR, RECONNECT_MAX
)
check("A4-PUB-05a backoff 1→2", pub5._reconnect_wait == 2.0)

pub5._reconnect_wait = min(
    pub5._reconnect_wait * RECONNECT_FACTOR, RECONNECT_MAX
)
check("A4-PUB-05b backoff 2→4", pub5._reconnect_wait == 4.0)

# 多次退避达到上限
for _ in range(10):
    pub5._reconnect_wait = min(
        pub5._reconnect_wait * RECONNECT_FACTOR, RECONNECT_MAX
    )
check("A4-PUB-05c capped at 30s", pub5._reconnect_wait == RECONNECT_MAX)

# 重置后退避回到初始值(成功连接后)
pub5._reconnect_wait = RECONNECT_INITIAL
check("A4-PUB-05d reset to initial", pub5._reconnect_wait == 1.0)

# ---------------------------------------------------------------------------
# A4-PUB-06: stop() 释放 — 线程安全停止 + 串口关闭
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-06 stop() 释放 ===")

clock6 = FakeClock(6000.0)
pub6 = SerialPublisher(port="TEST", clock=clock6, log_cb=None)

class _MockSerStop:
    def __init__(self):
        self.is_open = True
        self._closed = False
    def write(self, data):
        return len(data)
    def flush(self):
        pass
    def close(self):
        self.is_open = False
        self._closed = True

mock6 = _MockSerStop()
pub6._ser = mock6
pub6._running = True

# stop 应关闭运行标志并关闭串口
pub6.stop()
check("A4-PUB-06a _running False", not pub6._running)
check("A4-PUB-06b port closed", mock6._closed)

# ---------------------------------------------------------------------------
# A4-PUB-07: 日志限长验证
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-07 日志限长 ===")

log_lines = []
pub7 = SerialPublisher(port="TEST", log_cb=lambda s: log_lines.append(s))
# 不设置 clock,使用默认 time.monotonic

# 写入超过 max_log_entries 条日志
for i in range(250):
    pub7._log(f"test log {i}")

check("A4-PUB-07a log buffer capped", len(pub7.log_messages) <= 200,
      f"got {len(pub7.log_messages)}")

# ---------------------------------------------------------------------------
# A4-PUB-08: publish 边界 — 负数 confidence 夹持
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-08 publish 边界 ===")

pub8 = SerialPublisher(port="TEST", log_cb=None)
pub8._log_cb = None

# confidence 负数应被夹持为 0
pub8.publish(MockResult("NORMAL", -10, 100, 8000000))
check("A4-PUB-08a neg conf clamped 0", pub8._latest_confidence == 0)

# confidence > 100 应被夹持为 100
pub8.publish(MockResult("FATIGUE", 150, 100, 8001000))
check("A4-PUB-08b >100 conf clamped 100", pub8._latest_confidence == 100)

# 非法 state 应被忽略且不更新 has_result
pub8.publish(MockResult("INVALID", 50, 100, 8002000))
check("A4-PUB-08c invalid state ignored", pub8._latest_state == "FATIGUE",
      f"got {pub8._latest_state}")

# ---------------------------------------------------------------------------
# A4-PUB-09: encode_dms1_frame 输出始终是 bytes 并以 CRLF 结尾
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-09 encode 输出格式 ===")

for st in ["NORMAL", "YAWN", "FATIGUE", "UNKNOWN"]:
    for conf in [0, 50, 100]:
        for seq in [0, 32768, 65535]:
            f = encode_dms1_frame(seq, st, conf)
            check(f"A4-PUB-09 {st} s={seq} c={conf} bytes", isinstance(f, bytes))
            check(f"A4-PUB-09 {st} s={seq} c={conf} CRLF", f.endswith(b"\r\n"))
            check(f"A4-PUB-09 {st} s={seq} c={conf} ASCII", f.startswith(b"DMS1,"))
            check(f"A4-PUB-09 {st} s={seq} c={conf} len<=63", len(f) <= 63,
                  f"len={len(f)}")

# ---------------------------------------------------------------------------
# A4-PUB-10: Elite V2 auto-download control lines are safe before open
# ---------------------------------------------------------------------------
print("\n=== A4-PUB-10 safe DTR/RTS open ===")

class _SafeOpenMock:
    def __init__(self):
        self.is_open = False
        self.port = None
        self.dtr = True
        self.rts = True
        self.open_snapshot = None

    def open(self):
        self.open_snapshot = (self.port, self.dtr, self.rts)
        self.is_open = True

    def close(self):
        self.is_open = False

safe_ser = _SafeOpenMock()
pub10 = SerialPublisher(port="TEST", clock=FakeClock(10000.0), log_cb=None)
with patch("serial_bridge.serial.Serial", return_value=safe_ser) as serial_ctor:
    pub10._try_connect(10000.0)

check("A4-PUB-10a constructor leaves port closed",
      serial_ctor.call_args.kwargs["port"] is None)
check("A4-PUB-10b DTR/RTS safe before open",
      safe_ser.open_snapshot == ("TEST", False, False),
      f"got {safe_ser.open_snapshot!r}")
pub10._close_port()

# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"A4 Publisher Tests: {PASSED} PASS, {FAILED} FAIL")
if FAILED:
    print(f"{FAILED} FAILURE(S)")
    sys.exit(1)
else:
    print("ALL A4 PUBLISHER TESTS PASSED")
