"""
G3-01 A4 独立验证: 合法/非法/超长帧解析与构造

覆盖 protocol_dms1.md 第 2 节所有合法与非法报文类别。
使用实际 parse_dms1_frame() 和 encode_dms1_frame() 产品函数。
"""
import sys
import os

WORKDIR = os.path.join(os.path.dirname(__file__), "..", "..",
                       "host_app")
sys.path.insert(0, WORKDIR)

from serial_bridge import encode_dms1_frame, parse_dms1_frame, VALID_STATES

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
# A4-FRM-01: 合法帧 encode → parse 往返
# ======================================================================
print("=== A4-FRM-01 合法帧往返 ===")

LEGAL_VECTORS = [
    (0, "UNKNOWN", 0, b"DMS1,0,UNKNOWN,0\r\n"),
    (42, "NORMAL", 95, b"DMS1,42,NORMAL,95\r\n"),
    (43, "YAWN", 88, b"DMS1,43,YAWN,88\r\n"),
    (44, "FATIGUE", 91, b"DMS1,44,FATIGUE,91\r\n"),
    (65535, "NORMAL", 100, b"DMS1,65535,NORMAL,100\r\n"),
    (0, "NORMAL", 0, b"DMS1,0,NORMAL,0\r\n"),
    (10, "NORMAL", 0, b"DMS1,10,NORMAL,0\r\n"),
    (11, "NORMAL", 100, b"DMS1,11,NORMAL,100\r\n"),
    (65535, "FATIGUE", 100, b"DMS1,65535,FATIGUE,100\r\n"),
]

for seq, state, conf, expected in LEGAL_VECTORS:
    encoded = encode_dms1_frame(seq, state, conf)
    check(f"A4-FRM-01a encode {state} s={seq}",
          encoded == expected, f"got {encoded!r}")

    parsed = parse_dms1_frame(encoded)
    check(f"A4-FRM-01b parse {state} s={seq}",
          parsed is not None and
          parsed["seq"] == seq and
          parsed["state"] == state and
          parsed["confidence"] == conf,
          f"got {parsed}")

# 所有合法帧长度 ≤ 63 字节
for seq, state, conf, _ in LEGAL_VECTORS:
    f = encode_dms1_frame(seq, state, conf)
    check(f"A4-FRM-01c len<={63} {state} s={seq}", len(f) <= 63,
          f"len={len(f)}")

# ---------------------------------------------------------------------------
# A4-FRM-02: 帧头错误拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-02 帧头错误 ===")

BAD_HEADERS = [
    b"XMS1,0,NORMAL,95\r\n",
    b"DMS2,0,NORMAL,95\r\n",
    b"DMS,0,NORMAL,95\r\n",
    b" DMS1,0,NORMAL,95\r\n",   # 前导空格
    b"dms1,0,NORMAL,95\r\n",    # 小写
    b"DMS1,0,NORMAL,95\x00\r\n",  # 含 null 字节
]

for raw in BAD_HEADERS:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-02 reject {raw[:20]!r}", result is None,
          f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-03: 行尾错误拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-03 行尾错误 ===")

BAD_LINE_ENDING = [
    (b"DMS1,0,NORMAL,95\n", "缺 CR 仅 LF"),
    (b"DMS1,0,NORMAL,95\r", "缺 LF 仅 CR"),
    (b"DMS1,0,NORMAL,95", "无 CRLF"),
    (b"DMS1,0,NORMAL,95\n\r", "逆序 LFCR"),
]

for raw, desc in BAD_LINE_ENDING:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-03 {desc}", result is None, f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-04: state 不在枚举内拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-04 state 非法 ===")

BAD_STATES = [
    (b"DMS1,0,BOOT,0\r\n", "BOOT"),
    (b"DMS1,0,LINK_LOST,0\r\n", "LINK_LOST"),
    (b"DMS1,0,normal,0\r\n", "小写 normal"),
    (b"DMS1,0,NORMAL ,95\r\n", "尾部空格"),
    (b"DMS1,0, NORMAL,95\r\n", "前导空格"),
    (b"DMS1,0,,0\r\n", "空 state"),
    (b"DMS1,0,SLEEP,0\r\n", "未知状态 SLEEP"),
]

for raw, desc in BAD_STATES:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-04 reject {desc}", result is None,
          f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-05: confidence 越界拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-05 confidence 越界 ===")

BAD_CONF = [
    (b"DMS1,0,NORMAL,101\r\n", "101"),
    (b"DMS1,0,NORMAL,-1\r\n", "-1"),
    (b"DMS1,0,NORMAL,999\r\n", "999"),
    (b"DMS1,0,NORMAL,255\r\n", "255"),
]

for raw, desc in BAD_CONF:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-05 reject conf={desc}", result is None,
          f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-06: seq 非法拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-06 seq 非法 ===")

BAD_SEQ = [
    (b"DMS1,65536,NORMAL,95\r\n", "65536 越界"),
    (b"DMS1,-1,NORMAL,95\r\n", "-1"),
    (b"DMS1,abc,NORMAL,95\r\n", "abc 非数字"),
    (b"DMS1,1.5,NORMAL,95\r\n", "1.5 浮点数"),
    (b"DMS1,,NORMAL,95\r\n", "空 seq"),
    (b"DMS1,99999,NORMAL,95\r\n", "99999 越界"),
]

for raw, desc in BAD_SEQ:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-06 reject seq {desc}", result is None,
          f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-07: 字段数量错误拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-07 字段数量错误 ===")

BAD_FIELDS = [
    (b"DMS1,0,NORMAL\r\n", "缺 confidence 3字段"),
    (b"DMS1,0,NORMAL,95,extra\r\n", "多余字段 5字段"),
    (b"DMS1,0,95\r\n", "缺 state 3字段"),
    (b"DMS1,0,NORMAL,95,extra1,extra2\r\n", "6字段"),
]

for raw, desc in BAD_FIELDS:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-07 reject {desc}", result is None,
          f"got {result}")

# ---------------------------------------------------------------------------
# A4-FRM-08: 超长帧拒绝
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-08 超长帧拒绝 ===")

# 63 字节包含有效 CRLF,但字段解析应仍失败(内容非法)
f63 = b"DMS1,65535,FATIGUE,100" + b"x" * 36 + b"\r\n"
check(f"A4-FRM-08a len={len(f63)} 63 字节拒绝",
      parse_dms1_frame(f63) is None,
      f"got {parse_dms1_frame(f63)}")

# 64 字节超长帧
f64 = b"DMS1,65535,FATIGUE,100" + b"x" * 37 + b"\r\n"
check(f"A4-FRM-08b len={len(f64)} 64 字节拒绝",
      parse_dms1_frame(f64) is None,
      f"got {parse_dms1_frame(f64)}")

# 100 字节超长帧
f100 = b"DMS1,65535,FATIGUE,100" + b"x" * 73 + b"\r\n"
check(f"A4-FRM-08c len={len(f100)} 100 字节拒绝",
      parse_dms1_frame(f100) is None)

# 边界: 刚刚在 61 字节内的合法帧
f24 = b"DMS1,65535,FATIGUE,100\r\n"
check(f"A4-FRM-08d len=24 合法帧通过",
      parse_dms1_frame(f24) is not None,
      f"got {parse_dms1_frame(f24)}")

# 边界: 61 字节包含字段错误(内容非法)
f61 = b"DMS1,65535,FATIGUE,100" + b"x" * 34 + b"\r\n"
check(f"A4-FRM-08e len=61 字段错误拒绝",
      parse_dms1_frame(f61) is None)

# ---------------------------------------------------------------------------
# A4-FRM-09: 空帧/边界
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-09 空帧/边界 ===")

EDGE_CASES = [
    (b"", "完全空帧"),
    (b"\r\n", "仅 CRLF"),
]

for raw, desc in EDGE_CASES:
    result = parse_dms1_frame(raw)
    check(f"A4-FRM-09 reject {desc}", result is None,
          f"got {result}")

# 非 ASCII 编码
non_ascii = "DMS1,0,NORMAL,95\r\n".encode("ascii")  # 合法
check("A4-FRM-09a ASCII OK", parse_dms1_frame(non_ascii) is not None)

# GBK 编码应因 decode('ascii') 失败而拒绝
gbk_frame = b"DMS1,0,\xc3\xf1,95\r\n"  # non-ASCII bytes
check("A4-FRM-09b GBK bytes rejected",
      parse_dms1_frame(gbk_frame) is None)

# ---------------------------------------------------------------------------
# A4-FRM-10: 粘包解析
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-10 粘包解析 ===")

# 三帧粘在一起
sticky = b"DMS1,0,NORMAL,80\r\nDMS1,1,NORMAL,81\r\nDMS1,2,NORMAL,82\r\n"
lines = sticky.split(b"\r\n")
count = 0
for line in lines:
    if not line:
        continue
    full = line + b"\r\n"
    r = parse_dms1_frame(full)
    if r:
        count += 1
        check(f"A4-FRM-10a sticky frame #{count}",
              r["seq"] == count - 1, f"got seq={r['seq']}")

check("A4-FRM-10b all 3 parsed", count == 3, f"got {count}")

# 合法 + 非法 + 合法 粘包
mixed = b"DMS1,10,NORMAL,90\r\nDMS1,11,BOOT,0\r\nDMS1,12,NORMAL,91\r\n"
lines2 = mixed.split(b"\r\n")
valid = 0
for line in lines2:
    if not line:
        continue
    full = line + b"\r\n"
    r = parse_dms1_frame(full)
    if r:
        valid += 1
check("A4-FRM-10c mixed sticky: 2 valid", valid == 2, f"got {valid}")

# ---------------------------------------------------------------------------
# A4-FRM-11: encode 拒绝非法参数
# ---------------------------------------------------------------------------
print("\n=== A4-FRM-11 encode 拒绝非法参数 ===")

# seq 非法
for bad_seq in [-1, 65536, 99999, "abc", 1.5, None]:
    try:
        encode_dms1_frame(bad_seq, "NORMAL", 50)
        check(f"A4-FRM-11a seq={bad_seq!r} reject", False)
    except (ValueError, TypeError):
        check(f"A4-FRM-11a seq={bad_seq!r} reject", True)

# state 非法
for bad_st in ["BOOT", "LINK_LOST", "normal", "", " "]:
    try:
        encode_dms1_frame(0, bad_st, 50)
        check(f"A4-FRM-11b state={bad_st!r} reject", False)
    except ValueError:
        check(f"A4-FRM-11b state={bad_st!r} reject", True)

# confidence 非法
for bad_conf in [-1, 101, 255, 50.5, "abc", None]:
    try:
        encode_dms1_frame(0, "NORMAL", bad_conf)
        check(f"A4-FRM-11c conf={bad_conf!r} reject", False)
    except (ValueError, TypeError):
        check(f"A4-FRM-11c conf={bad_conf!r} reject", True)

# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"A4 Framing Tests: {PASSED} PASS, {FAILED} FAIL")
if FAILED:
    print(f"{FAILED} FAILURE(S)")
    sys.exit(1)
else:
    print("ALL A4 FRAMING TESTS PASSED")
