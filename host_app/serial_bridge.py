"""
DMS1 V1 串口发布器 —— 独立于推理/UI 线程的异步串口桥。

职责:
  - 以 2 Hz (500 ms) 发送 DMS1 协议帧。
  - seq 0-65535 循环递增并回绕。
  - 发布器启动后/首个有效结果前发送 UNKNOWN,0。
  - 推理故障或结果超过 1.5 秒陈旧时发送 UNKNOWN,0。
  - 断线自动重连(有上限指数退避)和限长日志。
  - 跨平台 COM 端口(/dev/ttyUSB* 与 COM*)。

不依赖 PySide6 或 Ultralytics;可独立用于测试和模拟。
"""

from __future__ import annotations

import json
import time
import threading
import logging
from typing import Callable, Optional
from collections import deque

# ---------------------------------------------------------------------------
# 可选 pyserial 导入 —— 仅在需要物理串口时
# ---------------------------------------------------------------------------
try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:  # pragma: no cover - 测试时可 mock
    serial = None  # type: ignore
    HAS_PYSERIAL = False

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
HEARTBEAT_PERIOD = 0.5          # 500 ms
STALE_TIMEOUT = 1.5             # 结果陈旧阈值(秒)
SEQ_MAX = 65535                 # seq 回绕上限
MAX_LOG_ENTRIES = 200           # 限长日志条数

# 重连退避参数
RECONNECT_INITIAL = 1.0         # 初始等待(秒)
RECONNECT_MAX = 30.0            # 上限(秒)
RECONNECT_FACTOR = 2.0          # 退避因子

VALID_STATES = {"NORMAL", "YAWN", "FATIGUE", "UNKNOWN"}

# 日志器
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 帧编码(独立函数,便于测试)
# ---------------------------------------------------------------------------

def encode_dms1_frame(seq: int, state: str, confidence: int) -> bytes:
    """将字段编码为 DMS1 V1 协议帧(含 CRLF)。

    参数:
        seq:        0-65535 循环序号
        state:      NORMAL|YAWN|FATIGUE|UNKNOWN
        confidence: 0-100 整数

    返回:
        bytes — 如 b'DMS1,42,NORMAL,95\\r\\n'

    异常:
        ValueError — 字段无效
    """
    if not isinstance(seq, int) or seq < 0 or seq > 65535:
        raise ValueError(f"seq 越界: {seq!r}")
    if state not in VALID_STATES:
        raise ValueError(f"state 无效: {state!r}, 允许 {sorted(VALID_STATES)}")
    if not isinstance(confidence, int) or confidence < 0 or confidence > 100:
        raise ValueError(f"confidence 越界: {confidence!r}")

    return f"DMS1,{seq},{state},{confidence}\r\n".encode("ascii")


def parse_dms1_frame(line: bytes) -> Optional[dict]:
    """解析 DMS1 V1 帧为字段字典;非法帧返回 None。

    用于测试向量和模拟器;STM32 侧自行实现 C 语言解析器。
    """
    if not line.endswith(b"\r\n"):
        return None
    line = line[:-2]  # 去掉 CRLF
    if len(line) > 61:  # max_frame_bytes=63,减去 CRLF 后最大 61
        return None
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None

    parts = text.split(",")
    if len(parts) != 4:
        return None
    if parts[0] != "DMS1":
        return None

    # seq
    try:
        seq = int(parts[1])
    except ValueError:
        return None
    if seq < 0 or seq > 65535:
        return None

    # state
    state = parts[2]
    if state not in VALID_STATES:
        return None

    # confidence
    try:
        confidence = int(parts[3])
    except ValueError:
        return None
    if confidence < 0 or confidence > 100:
        return None

    return {"seq": seq, "state": state, "confidence": confidence}


# ---------------------------------------------------------------------------
# CHG-G5-001: JSONL 遥测写入器(标准库,默认关闭,异常隔离)
# ---------------------------------------------------------------------------

class JsonlEventWriter:
    """事件 JSONL 追加写入器(仅标准库)。

    仅当 DMS1_EVENT_LOG 显式设置时由调用方创建并作为 event_cb 传入;
    未设置时不得实例化(避免产生任何文件)。

    特性:
      - UTF-8 追加模式("a")打开:每次写入追加到文件末尾,不覆盖已有内容。
        会话文件路径唯一是使用者责任(每次真实 G5 测试会话用新文件)。
      - 紧凑 JSON 单行(separators=(",", ":")),newline="\\n"。
      - 不逐行 flush,close() 时统一 flush/close,避免拖慢发送线程。
      - 写入/关闭全异常隔离:任何失败静默丢弃并计数,不向调用方抛异常。
      - 无内部加锁/睡眠(契约:回调内不得 sleep/加锁)。
    """

    def __init__(self, path: str):
        self._path = str(path)
        self._closed = False
        self._wrote = 0
        self._failures = 0
        self._file = open(self._path, "a", encoding="utf-8", newline="\n")

    @property
    def path(self) -> str:
        return self._path

    @property
    def wrote(self) -> int:
        return self._wrote

    @property
    def failures(self) -> int:
        return self._failures

    def write(self, event: dict) -> bool:
        """写入一个事件对象;返回是否已写入(关闭/失败返回 False)。"""
        if self._closed:
            return False
        try:
            line = json.dumps(event, separators=(",", ":")) + "\n"
            self._file.write(line)
            self._wrote += 1
            return True
        except Exception:
            self._failures += 1
            return False

    def close(self) -> None:
        """关闭文件(幂等),flush 并释放句柄。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            self._failures += 1


# ---------------------------------------------------------------------------
# 发布器
# ---------------------------------------------------------------------------

class SerialPublisher:
    """DMS1 串口发布器。

    用法:
        pub = SerialPublisher(port="COM3", log_cb=print)
        pub.start()
        ...
        pub.publish(result)       # FatigueResult 或兼容命名元组
        pub.stop()
    """

    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = 115200,
        heartbeat_period: float = HEARTBEAT_PERIOD,
        stale_timeout: float = STALE_TIMEOUT,
        reconnect_initial: float = RECONNECT_INITIAL,
        reconnect_max: float = RECONNECT_MAX,
        reconnect_factor: float = RECONNECT_FACTOR,
        max_log_entries: int = MAX_LOG_ENTRIES,
        log_cb: Optional[Callable[[str], None]] = None,
        event_cb: Optional[Callable[[dict], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        port:    串口名称(Windows "COM3", Linux "/dev/ttyUSB0")
        baudrate: 波特率,默认 115200
        heartbeat_period: 心跳周期(秒),默认 0.5
        stale_timeout: 结果陈旧阈值(秒),默认 1.5
        reconnect_initial: 重连初始等待(秒),默认 1.0
        reconnect_max: 重连最大等待(秒),默认 30.0
        reconnect_factor: 退避因子,默认 2.0
        max_log_entries: 限长日志条数,默认 200
        log_cb:   日志回调(str) -> None;None 则使用标准 logging
        event_cb: CHG-G5-001 可选遥测回调(dict) -> None;None 表示遥测关闭。
                  start/stop/connect/connect_failed/disconnect/frame 事件均经此发出。
                  回调异常被吞掉,绝不中断发送线程或改变 seq/心跳。
        clock:    可注入单调时钟(测试用)
        """
        self.port = port
        self.baudrate = baudrate
        self._heartbeat_period = heartbeat_period
        self._stale_timeout = stale_timeout
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._reconnect_factor = reconnect_factor
        self._max_log_entries = max_log_entries
        self._log_cb = log_cb
        self._event_cb = event_cb
        self._clock = clock

        # 线程安全
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 最新结果
        self._latest_state = "UNKNOWN"
        self._latest_confidence = 0
        self._latest_timestamp_ms: Optional[int] = None  # 首次发布前为 None
        self._latest_duration_ms = 0  # CHG-G5-001: 当前状态已持续毫秒
        self._has_result = False

        # 序号
        self._seq = 0

        # 串口句柄
        self._ser: Optional[serial.Serial] = None

        # 重连状态
        self._reconnect_wait = self._reconnect_initial
        self._last_connect_attempt = 0.0

        # 限长日志
        self._log_buffer: deque = deque(maxlen=self._max_log_entries)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[Serial {ts}] {msg}"
        self._log_buffer.append(line)
        if self._log_cb:
            try:
                self._log_cb(line)
            except Exception:
                pass
        _log.info(msg)

    @property
    def log_messages(self):
        """返回最近日志列表(最新在前)。"""
        return list(self._log_buffer)

    # ------------------------------------------------------------------
    # CHG-G5-001: 遥测事件(只读观测,默认关闭)
    # ------------------------------------------------------------------

    def _emit_event(self, payload: dict):
        """构造 schema 基础字段并回调;回调异常绝不抛向发送线程。

        payload 必须含 "type";基础字段 mono_ms/wall_ms 由本方法补全。
        """
        if self._event_cb is None:
            return
        event = {
            "type": payload["type"],
            "mono_ms": int(self._clock() * 1000),
            "wall_ms": int(time.time() * 1000),
        }
        event.update(payload)
        try:
            self._event_cb(event)
        except Exception:
            pass

    def _frame_event(self, state: str, confidence: int, now: float) -> dict:
        """构造 frame 事件附加字段(在串口写成功后、seq 自增前调用)。

        CHG-G5-001: visual 是 publish(result) 传入的最近原始 FatigueState
        规范字符串,visual_ms/duration_ms 是同一 result 的 timestamp_ms/
        duration_ms。state 是本次实际写出帧的状态;因此 visual != state
        只可能来自真实的首帧(尚无 result,visual 初始 UNKNOWN)或陈旧降级
        (state 降为 UNKNOWN,visual 仍为最近原始状态),不做人为恒不等映射。
        """
        with self._lock:
            has = self._has_result
            raw_state = self._latest_state
            ts = self._latest_timestamp_ms
            dur = self._latest_duration_ms
        if has:
            visual_ms = ts
            duration_ms = dur
        else:
            visual_ms = int(now * 1000)
            duration_ms = 0
        return {
            "type": "frame",
            "seq": self._seq,
            "state": state,
            "confidence": confidence,
            "visual": raw_state,
            "visual_ms": visual_ms,
            "duration_ms": duration_ms,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动发布线程(daemon)。"""
        if self._running:
            return
        self._running = True
        self._emit_event({"type": "start"})
        self._thread = threading.Thread(target=self._run, daemon=True, name="DMS1-Pub")
        self._thread.start()
        self._log(f"发布器启动 port={self.port} baud={self.baudrate}")

    def stop(self):
        """停止发布线程并释放串口。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._close_port()
        self._log("发布器已停止")
        self._emit_event({"type": "stop"})

    # ------------------------------------------------------------------
    # 发布接口(线程安全)
    # ------------------------------------------------------------------

    def publish(self, result) -> None:
        """发布最新 FatigueResult。

        result 需提供属性: state(或 .state.value 枚举), evidence_score, timestamp_ms。
        兼容 FatigueResult 命名元组和 FatigueState 枚举。
        """
        with self._lock:
            # 提取 state 字符串
            raw_state = getattr(result, "state", None)
            if raw_state is None:
                self._log("publish: result 缺少 state 属性")
                return
            # 兼容枚举 .value 或直接字符串
            if hasattr(raw_state, "value"):
                state = raw_state.value
            else:
                state = str(raw_state)
            if state not in VALID_STATES:
                self._log(f"publish: 非法 state={state}, 忽略")
                return

            score = int(getattr(result, "evidence_score", 0))
            if score < 0:
                score = 0
            elif score > 100:
                score = 100

            self._latest_state = state
            self._latest_confidence = score
            self._latest_timestamp_ms = int(getattr(result, "timestamp_ms", 0))
            self._latest_duration_ms = int(getattr(result, "duration_ms", 0))
            self._has_result = True

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self):
        """发布线程主循环: 确保连接、编码帧、发送、睡眠。"""
        next_beat = self._clock()
        while self._running:
            now = self._clock()

            # 确保串口连接
            if not self._is_connected():
                self._try_connect(now)
                # 无论连接成功与否,按周期继续
                next_beat = now + self._heartbeat_period
                self._sleep_until(next_beat)
                continue

            # 获取当前应发送的状态
            state, confidence = self._current_state(now)

            # 编码并发送
            try:
                frame = encode_dms1_frame(self._seq, state, confidence)
                self._ser.write(frame)  # type: ignore[union-attr]
                self._ser.flush()       # type: ignore[union-attr]
                self._emit_event(self._frame_event(state, confidence, now))
                self._seq = (self._seq + 1) % (SEQ_MAX + 1)
            except (serial.SerialException, OSError) as exc:
                self._log(f"发送失败: {exc}")
                self._close_port()
                self._reconnect_wait = self._reconnect_initial

            next_beat += self._heartbeat_period
            # 防止积压:若编码/发送耗时超出一个周期则跳过
            if self._clock() > next_beat:
                next_beat = self._clock() + self._heartbeat_period
            self._sleep_until(next_beat)

    def _current_state(self, now: float) -> tuple:
        """决定当前应发送的 state 和 confidence。

        规则:
        - 尚无结果 -> UNKNOWN,0
        - 结果陈旧(超过 STALE_TIMEOUT) -> UNKNOWN,0
        - 正常 -> latest_state, latest_confidence
        """
        with self._lock:
            has = self._has_result
            ts = self._latest_timestamp_ms
            state = self._latest_state
            conf = self._latest_confidence

        if not has:
            return ("UNKNOWN", 0)

        # 将 timestamp_ms 转换为秒级 age
        if ts is not None:
            age = now - ts / 1000.0
            if age >= self._stale_timeout:
                return ("UNKNOWN", 0)

        return (state, conf)

    # ------------------------------------------------------------------
    # 串口连接管理
    # ------------------------------------------------------------------

    def _is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _try_connect(self, now: float):
        """尝试打开串口,失败时使用退避延迟。"""
        if now - self._last_connect_attempt < self._reconnect_wait:
            return
        self._last_connect_attempt = now

        if not HAS_PYSERIAL:
            self._log("pyserial 未安装,无法打开串口")
            return

        try:
            ser = serial.Serial(
                port=None,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,         # 非阻塞写入
                write_timeout=0.5,   # 写入超时
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # ponytail: Elite V2 routes these lines into BOOT0/RESET; set them before open.
            ser.dtr = False
            ser.rts = False
            ser.port = self.port
            ser.open()
            self._ser = ser
            self._log(f"串口已连接 {self.port}")
            self._reconnect_wait = self._reconnect_initial  # 重置退避
            self._emit_event({"type": "connect", "port": self.port})
        except (serial.SerialException, OSError) as exc:
            self._log(f"串口连接失败 {self.port}: {exc}")
            self._reconnect_wait = min(
                self._reconnect_wait * self._reconnect_factor, self._reconnect_max
            )
            self._emit_event({
                "type": "connect_failed",
                "port": self.port,
                "wait": int(self._reconnect_wait * 1000),
            })

    def _close_port(self):
        """安全关闭串口。"""
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
            self._emit_event({"type": "disconnect", "port": self.port})

    def _sleep_until(self, target: float):
        """精度睡眠到目标时刻,最多按心跳周期拆分以响应停止信号。"""
        while self._running:
            remain = target - self._clock()
            if remain <= 0:
                return
            # 每次最多睡 100 ms,确保及时响应 stop()
            sleep_ms = min(remain, 0.1)
            time.sleep(sleep_ms)
