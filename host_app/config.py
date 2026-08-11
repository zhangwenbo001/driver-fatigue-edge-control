"""
配置文件 - 全局常量和路径设置
"""

import os

# 关闭YOLO调试输出
os.environ['YOLO_VERBOSE'] = 'False'

# 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 模型路径（文件夹内部相对路径）
MODEL_PATH = os.path.join(BASE_DIR, 'models', 'best.pt')

# 检测参数
DEFAULT_CONFIDENCE = 0.25       # 默认置信度阈值
FRAME_QUEUE_SLEEP = 0.01        # 帧队列空时休眠(秒)
DETECTION_SLEEP = 0.05          # 检测循环休眠(秒)
CLOSED_EYE_FATIGUE_SECONDS = 1.5  # 连续闭眼达到该时长才判定疲劳
CLOSED_EYE_GAP_SECONDS = 0.3      # 容忍眼睛目标短暂漏检，睁眼仍立即清零
CAMERA_TIMER_INTERVAL = 33      # 摄像头定时器间隔(毫秒) ≈ 30fps
CAMERA_WIDTH = 520              # 显示宽度
CAMERA_HEIGHT = 400             # 显示高度

# 窗口设置
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 700
WINDOW_TITLE = "Driver Fatigue Detection System"

# ── 串口配置 ──
# 默认 COM 口（Windows "COM3", Linux "/dev/ttyUSB0"）
# 可通过命令行参数或环境变量 DMS1_PORT 覆盖
SERIAL_PORT = "COM3"
SERIAL_BAUDRATE = 115200
SERIAL_HEARTBEAT_PERIOD = 0.5       # 500 ms (2 Hz)
SERIAL_RESULT_STALE_TIMEOUT = 1.5   # 结果超过此秒数发送 UNKNOWN,0
SERIAL_RECONNECT_INITIAL = 1.0      # 重连初始等待(秒)
SERIAL_RECONNECT_MAX = 30.0         # 重连最大等待(秒)
SERIAL_RECONNECT_FACTOR = 2.0       # 退避因子
SERIAL_MAX_LOG_ENTRIES = 200        # 发布器限长日志条数

# ── 线程与队列 ──
FRAME_QUEUE_SIZE = 1                # queue.Queue maxsize(只保留最新帧)
INFERENCE_TIMEOUT = 5.0             # 推理线程join超时(秒)
