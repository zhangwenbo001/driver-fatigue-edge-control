"""G0-01 硬件补证集成测试 (A4)
ponytail: 直接使用标准库 subprocess + json，无测试框架依赖。
安全边界：仅操作精英V2/USB_UART/摄像头；不连接或控制真实车辆/继电器/执行器。
"""
import subprocess
import json
import sys
import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_ps(cmd: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout, cwd=PROJECT_ROOT,
        )
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def run_py(script: str, timeout: int = 15) -> tuple:
    try:
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=timeout, cwd=PROJECT_ROOT,
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "", -1


def main():
    results = []
    ts = datetime.now().isoformat()

    # 1. COM port enumeration
    ports = run_ps("[System.IO.Ports.SerialPort]::GetPortNames()")
    has_com3 = "COM3" in ports
    results.append({
        "test": "COM3_ENUMERATED",
        "status": "PASS" if has_com3 else "FAIL",
        "actual": ports,
        "expected": "COM3 in port list",
    })

    # 2. COM3 device identity (CH340)
    dev_info = run_ps(
        "Get-WmiObject Win32_PnPEntity | Where-Object { $_.Name -match 'COM3' } | "
        "Select-Object -ExpandProperty Name"
    )
    is_ch340 = "CH340" in dev_info.upper() if dev_info else False
    results.append({
        "test": "COM3_IDENTITY_CH340",
        "status": "PASS" if is_ch340 else "FAIL",
        "actual": dev_info,
        "expected": "USB-SERIAL CH340 (COM3)",
    })

    # 3. Camera enumeration
    cam_script = (
        "import cv2; caps = [(i, cv2.VideoCapture(i).isOpened()) for i in range(5)]; "
        "[cv2.VideoCapture(i[0]).release() for i in caps]; "
        "print([f'camera[{i}]: {\"OK\" if ok else \"FAIL\"}' for i, ok in caps])"
    )
    cam_out, _, _ = run_py(cam_script)
    cam_ok = "OK" in cam_out
    results.append({
        "test": "CAMERA_AVAILABLE",
        "status": "PASS" if cam_ok else "FAIL",
        "actual": cam_out,
        "expected": "at least one camera available",
    })

    # 4. Serial port open/close test
    serial_script = (
        "import serial; "
        "s = serial.Serial(port=None, baudrate=115200, timeout=1, xonxoff=False, rtscts=False, dsrdtr=False); "
        "s.dtr = False; s.rts = False; s.port = 'COM3'; s.open(); "
        "ok_open = s.is_open; "
        "s.close(); "
        "ok_closed = not s.is_open; "
        "print(f'OPEN={ok_open} CLOSE={ok_closed}')"
    )
    serial_out, serial_err, _ = run_py(serial_script)
    open_ok = "OPEN=True" in serial_out
    close_ok = "CLOSE=True" in serial_out
    results.append({
        "test": "COM3_OPEN_CLOSE",
        "status": "PASS" if (open_ok and close_ok) else "FAIL",
        "actual": f"stdout={serial_out}, stderr={serial_err}",
        "expected": "OPEN=True CLOSE=True",
    })

    # 5. Legal DMS1 frame send
    tx_script = (
        "import serial; "
        "s = serial.Serial(port=None, baudrate=115200, timeout=1, xonxoff=False, rtscts=False, dsrdtr=False); "
        "s.dtr = False; s.rts = False; s.port = 'COM3'; s.open(); "
        "frames = [b'DMS1,0,UNKNOWN,0\\r\\n', b'DMS1,1,NORMAL,95\\r\\n', b'DMS1,2,YAWN,88\\r\\n', b'DMS1,3,FATIGUE,91\\r\\n']; "
        "results = [(s.write(f), f) for f in frames]; "
        "s.close(); "
        "[print(f'wrote {n} bytes: {f!r}') for n, f in results]"
    )
    tx_out, tx_err, tx_rc = run_py(tx_script)
    tx_ok = tx_rc == 0 and "wrote" in tx_out
    results.append({
        "test": "DMS1_LEGAL_FRAMES_SENT",
        "status": "PASS" if tx_ok else "FAIL",
        "actual": f"stdout={tx_out}, stderr={tx_err}, rc={tx_rc}",
        "expected": "4 frames written successfully",
    })

    # 6. Illegal DMS1 frame send
    illegal_script = (
        "import serial; "
        "s = serial.Serial(port=None, baudrate=115200, timeout=1, xonxoff=False, rtscts=False, dsrdtr=False); "
        "s.dtr = False; s.rts = False; s.port = 'COM3'; s.open(); "
        "n = s.write(b'XMS1,0,BOOT,0\\r\\n'); "
        "s.close(); "
        "print(f'wrote {n} bytes: illegal frame')"
    )
    il_out, il_err, il_rc = run_py(illegal_script)
    il_ok = il_rc == 0
    results.append({
        "test": "DMS1_ILLEGAL_FRAME_SENT",
        "status": "PASS" if il_ok else "FAIL",
        "actual": f"stdout={il_out}, stderr={il_err}, rc={il_rc}",
        "expected": "illegal frame written to COM3",
    })

    # Summary
    passed = sum(1 for r in results if r["status"] == "PASS")
    total = len(results)

    print(f"=== G0-01 HARDWARE EVIDENCE SUMMARY ({ts}) ===")
    print(f"PASS: {passed}/{total}")
    for r in results:
        flag = "[PASS]" if r["status"] == "PASS" else "[FAIL]"
        print(f"{flag} {r['test']}: {r['actual'][:120]}")
    
    # Write JSON evidence
    evidence_dir = os.path.join(PROJECT_ROOT, "evidence", "G0", "G0-01")
    os.makedirs(evidence_dir, exist_ok=True)
    json_path = os.path.join(evidence_dir, "hardware_evidence.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"date": ts, "results": results, "summary": f"{passed}/{total}"}, f, indent=2, ensure_ascii=False)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
