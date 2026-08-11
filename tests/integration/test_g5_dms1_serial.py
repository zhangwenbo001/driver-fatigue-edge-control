"""G5 硬件集成测试 — DMS1 帧端到端测试 (仅 COM3, 115200 8N1)
不操作车辆控制设备。不修改固件或主机源码。
"""
import serial
import time
import json
from datetime import datetime, timezone

PORT = "COM3"
BAUD = 115200
RESULT_FILE = "evidence/G5/G5-01-TEST/g5_serial_test.json"

LEGAL_SEQUENCE = [
    ("NORMAL",  b"DMS1,0,NORMAL,95\r\n"),
    ("YAWN",    b"DMS1,1,YAWN,88\r\n"),
    ("FATIGUE", b"DMS1,2,FATIGUE,91\r\n"),
    ("UNKNOWN", b"DMS1,3,UNKNOWN,0\r\n"),
]

MALFORMED_SEQUENCE = [
    ("BAD_HEADER",    b"XMS1,0,NORMAL,95\r\n"),
    ("NO_CR",         b"DMS1,0,NORMAL,95\n"),
    ("NO_LF",         b"DMS1,0,NORMAL,95\r"),
    ("STATE_BOOT",    b"DMS1,0,BOOT,0\r\n"),
    ("CONF_101",      b"DMS1,0,NORMAL,101\r\n"),
    ("MISSING_FIELD", b"DMS1,0,NORMAL\r\n"),
    ("EXTRA_FIELD",   b"DMS1,0,NORMAL,95,EXTRA\r\n"),
]

RECOVERY_FRAME = b"DMS1,100,NORMAL,95\r\n"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def log(entries, level, msg):
    entry = {"ts": timestamp(), "level": level, "msg": msg}
    entries.append(entry)
    print(f"[{level}] {msg}")


def main():
    entries = []
    log(entries, "INFO", f"G5 DMS1 串口测试开始 — {PORT} @ {BAUD} 8N1")

    try:
        ser = serial.Serial(
            port=None, baudrate=BAUD,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=0.5,
            xonxoff=False, rtscts=False, dsrdtr=False,
        )
        ser.dtr = False
        ser.rts = False
        ser.port = PORT
        ser.open()
        log(entries, "PASS", f"端口 {PORT} 打开成功")
    except Exception as e:
        log(entries, "FAIL", f"无法打开 {PORT}: {e}")
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump({"test": "G5_DMS1_SERIAL", "status": "FAIL", "log": entries}, f, indent=2, ensure_ascii=False)
        return

    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # --- Phase 1: 合法帧序列 ---
    log(entries, "PHASE", "Phase 1: 合法帧序列 (NORMAL→YAWN→FATIGUE→UNKNOWN)")
    for label, frame in LEGAL_SEQUENCE:
        try:
            n = ser.write(frame)
            ser.flush()
            log(entries, "TX", f"{label}: 写入 {n} 字节 — {frame!r}")
        except Exception as e:
            log(entries, "FAIL", f"{label}: 写入失败 — {e}")
        time.sleep(0.6)  # 帧间间隔 >500ms (主机侧 2Hz 周期)

    # --- Phase 2: 非法帧序列 ---
    log(entries, "PHASE", "Phase 2: 非法帧序列 (应被拒绝，不更新状态)")
    for label, frame in MALFORMED_SEQUENCE:
        try:
            n = ser.write(frame)
            ser.flush()
            log(entries, "TX", f"{label}: 写入 {n} 字节 — {frame!r}")
        except Exception as e:
            log(entries, "FAIL", f"{label}: 写入失败 — {e}")
        time.sleep(0.3)

    # --- Phase 3: LINK_LOST 测试 ---
    log(entries, "PHASE", "Phase 3: LINK_LOST — 停止发送 2500ms")
    time.sleep(2.5)
    log(entries, "INFO", "2.5s 静默期结束，STM32 应已进入 LINK_LOST")

    # --- Phase 4: 恢复帧 ---
    log(entries, "PHASE", "Phase 4: 恢复帧")
    try:
        n = ser.write(RECOVERY_FRAME)
        ser.flush()
        log(entries, "TX", f"恢复帧: 写入 {n} 字节 — {RECOVERY_FRAME!r}")
    except Exception as e:
        log(entries, "FAIL", f"恢复帧写入失败 — {e}")
    time.sleep(0.6)

    # --- 收尾: 回到 NORMAL ---
    try:
        ser.write(b"DMS1,101,NORMAL,95\r\n")
        ser.flush()
        log(entries, "TX", "最终 NORMAL 帧已发送")
    except Exception as e:
        log(entries, "FAIL", f"最终帧写入失败 — {e}")

    ser.close()
    log(entries, "INFO", "端口已关闭")

    log(entries, "DONE", "G5 测试帧发送完成。LED/BEEP 行为需项目所有者目视确认。")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump({"test": "G5_DMS1_SERIAL", "status": "COMPLETE", "log": entries}, f, indent=2, ensure_ascii=False)
    print(f"\n结果已写入 {RESULT_FILE}")


if __name__ == "__main__":
    main()
