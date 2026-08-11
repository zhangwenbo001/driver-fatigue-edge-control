"""
程序入口 —— 启动疲劳检测 GUI 应用 (G3-01)

用法:
    python main.py [--port COM3] [--baud 115200]

    不传参数时使用 config.py 默认值(SERIAL_PORT / SERIAL_BAUDRATE);
    环境变量 DMS1_PORT 可覆盖默认端口。
"""

import argparse
import os
import sys

from PySide6 import QtWidgets
from config import SERIAL_PORT, SERIAL_BAUDRATE
from ui import MWindow


def _parse_args():
    p = argparse.ArgumentParser(description="疲劳检测系统 DMS1 V1")
    p.add_argument("--port", default=os.environ.get("DMS1_PORT", SERIAL_PORT),
                   help=f"串口名称 (默认 {SERIAL_PORT})")
    p.add_argument("--baud", type=int, default=SERIAL_BAUDRATE,
                   help=f"波特率 (默认 {SERIAL_BAUDRATE})")
    return p.parse_args()


def main():
    args = _parse_args()
    app = QtWidgets.QApplication(sys.argv)
    window = MWindow(serial_port=args.port, serial_baudrate=args.baud)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
