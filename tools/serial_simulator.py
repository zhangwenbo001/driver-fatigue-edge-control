"""DMS1 串口模拟器 — A4 测试与系统集成工具
ponytail: 直接使用 pyserial + 命令行参数，无框架依赖。
安全边界：仅操作指定 COM 口，不连接或控制真实车辆、继电器或外部执行器。
"""
import serial
import serial.tools.list_ports
import time
import json
import argparse
import sys
from datetime import datetime


DMS1_LEGAL_FRAMES = {
    "UNKNOWN_0": b"DMS1,0,UNKNOWN,0\r\n",
    "NORMAL_95": b"DMS1,1,NORMAL,95\r\n",
    "YAWN_88": b"DMS1,2,YAWN,88\r\n",
    "FATIGUE_91": b"DMS1,3,FATIGUE,91\r\n",
}

DMS1_ILLEGAL_FRAMES = {
    "BAD_HEADER": b"XMS1,0,BOOT,0\r\n",
    "NO_CR": b"DMS1,0,NORMAL,95\n",
    "NO_LF": b"DMS1,0,NORMAL,95\r",
    "STATE_BOOT": b"DMS1,0,BOOT,0\r\n",
    "CONF_101": b"DMS1,0,NORMAL,101\r\n",
    "MISSING_FIELD": b"DMS1,0,NORMAL\r\n",
    "EXTRA_FIELD": b"DMS1,0,NORMAL,95,extra\r\n",
}


def list_ports():
    ports = serial.tools.list_ports.comports()
    return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in ports]


def open_port(port: str, baud: int = 115200, timeout: float = 1.0):
    ser = serial.Serial(
        port=None,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    ser.dtr = False
    ser.rts = False
    ser.port = port
    ser.open()
    return ser


def send_and_log(ser, frame: bytes, label: str, log_entries: list):
    try:
        n = ser.write(frame)
        ser.flush()
        log_entries.append({"step": f"TX_{label}", "status": "PASS", "detail": f"wrote {n} bytes: {frame!r}"})
    except Exception as e:
        log_entries.append({"step": f"TX_{label}", "status": "FAIL", "detail": str(e)})
    time.sleep(0.1)


def drain_rx(ser, log_entries: list, wait_s: float = 0.5):
    time.sleep(wait_s)
    rx = b""
    while ser.in_waiting > 0:
        chunk = ser.read(ser.in_waiting)
        rx += chunk
        time.sleep(0.05)
    if rx:
        log_entries.append({"step": "RX_DATA", "status": "PASS", "detail": f"received {len(rx)} bytes: {rx!r}"})
    else:
        log_entries.append({"step": "RX_DATA", "status": "NOT_DETECTED", "detail": "no response received"})
    return rx


def main():
    parser = argparse.ArgumentParser(description="DMS1 Serial Simulator (A4)")
    parser.add_argument("--port", default="COM3", help="Serial port name")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--list", action="store_true", help="List available ports and exit")
    parser.add_argument("--legal-only", action="store_true", help="Send only legal frames")
    parser.add_argument("--illegal-only", action="store_true", help="Send only illegal frames")
    parser.add_argument("--output", "-o", help="Write JSON result to file")
    args = parser.parse_args()

    if args.list:
        ports = list_ports()
        print(json.dumps(ports, indent=2, ensure_ascii=False))
        return

    result = {
        "test_id": "DMS1_SERIAL_SIMULATOR",
        "date": datetime.now().isoformat(),
        "port": args.port,
        "baud": args.baud,
        "config": "115200 8N1 no flow control",
        "steps": [],
    }

    try:
        ser = open_port(args.port, args.baud)
        result["steps"].append({"step": "OPEN_PORT", "status": "PASS", "detail": f"{args.port} opened at {args.baud} 8N1"})
    except Exception as e:
        result["steps"].append({"step": "OPEN_PORT", "status": "FAIL", "detail": str(e)})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(1)

    if not ser.is_open:
        result["steps"].append({"step": "PORT_IS_OPEN", "status": "FAIL", "detail": "port not open"})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(1)

    result["steps"].append({"step": "PORT_IS_OPEN", "status": "PASS", "detail": "is_open=True"})

    ser.reset_input_buffer()
    ser.reset_output_buffer()
    result["steps"].append({"step": "FLUSH", "status": "PASS", "detail": "buffers flushed"})

    if not args.illegal_only:
        for label, frame in DMS1_LEGAL_FRAMES.items():
            send_and_log(ser, frame, label, result["steps"])

    if not args.legal_only:
        for label, frame in DMS1_ILLEGAL_FRAMES.items():
            send_and_log(ser, frame, label, result["steps"])

    drain_rx(ser, result["steps"], wait_s=0.5)

    try:
        ser.close()
        result["steps"].append({"step": "CLOSE_PORT", "status": "PASS", "detail": "port closed"})
    except Exception as e:
        result["steps"].append({"step": "CLOSE_PORT", "status": "FAIL", "detail": str(e)})

    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    print(output)


if __name__ == "__main__":
    main()
