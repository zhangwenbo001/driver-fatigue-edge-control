"""
GUI 界面模块 —— 主窗口 UI、摄像头管理、线程安全检测与串口集成。

G3-01B 正式接入:
  - 帧队列 queue.Queue(maxsize=1)，防止无界增长。
  - 后台推理线程通过 Qt 信号回主线程更新 UI，不直接操作控件。
  - 集成 SerialPublisher，以 2 Hz 发送 DMS1 V1 帧。
  - 正式接入 FatigueStateMachine，发送 NORMAL/YAWN/FATIGUE/UNKNOWN 四状态。
  - 摄像头 EOF/推理异常/线程退出自动降级 UNKNOWN。
  - 连续启停摄像头正确释放资源。
"""

from __future__ import annotations

import queue
import html
import os
import time
import traceback
from dataclasses import dataclass
from threading import Thread, Event

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtCore import Signal, QTimer

from config import (
    CAMERA_HEIGHT, CAMERA_TIMER_INTERVAL, CAMERA_WIDTH,
    FRAME_QUEUE_SIZE, FRAME_QUEUE_SLEEP,
    INFERENCE_TIMEOUT, MODEL_PATH,
    SERIAL_PORT, SERIAL_BAUDRATE,
    SERIAL_HEARTBEAT_PERIOD, SERIAL_RESULT_STALE_TIMEOUT,
    SERIAL_RECONNECT_INITIAL, SERIAL_RECONNECT_MAX, SERIAL_RECONNECT_FACTOR,
    SERIAL_MAX_LOG_ENTRIES,
    WINDOW_HEIGHT, WINDOW_TITLE, WINDOW_WIDTH,
)
from detector import FatigueDetector
from fatigue_state import (
    FatigueStateMachine, FatigueResult, FatigueState, YawnSnapshot,
)
from serial_bridge import SerialPublisher, JsonlEventWriter, VALID_STATES

# ---------------------------------------------------------------------------
# Qt 信号定义
# ---------------------------------------------------------------------------

class _DetectionSignals(QtCore.QObject):
    """跨线程信号容器。"""
    detection_ready = Signal(object)    # (frame_rgb, detections, FatigueResult)
    serial_log = Signal(str)            # 串口日志行
    inference_error = Signal(str)       # 推理异常消息


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MWindow(QtWidgets.QMainWindow):
    """疲劳检测系统主窗口 (G3-01 改造版)"""

    def __init__(self, serial_port: str = SERIAL_PORT,
                 serial_baudrate: int = SERIAL_BAUDRATE):
        super().__init__()
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setWindowTitle(WINDOW_TITLE)

        # ---------- 实例变量 ----------
        self.cap: cv2.VideoCapture | None = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)

        # ponytail: Q-01/Q-02 仅在显式环境变量下采样，避免产品默认路径增加 I/O。
        self._runtime_metrics_writer = None
        self._runtime_metrics_started_at = None
        self._runtime_metrics_window_frames = 0
        self._runtime_metrics_total_frames = 0

        # E2E08 专用：仅显式设定持续时间时，才在首个成功推理后注入一次故障。
        self._test_inference_fault_delay_s = self._test_seconds(
            "DMS1_TEST_INFERENCE_FAILURE_DELAY_S", 5.0)
        self._test_inference_fault_duration_s = self._test_seconds(
            "DMS1_TEST_INFERENCE_FAILURE_DURATION_S", 0.0)
        self._test_inference_ready_at = None
        self._test_inference_fault_until = None
        self._test_inference_fault_done = False

        # 检测器
        self.detector = FatigueDetector(MODEL_PATH)

        # 疲劳状态机 (G3-01B 正式接入)
        self._state_machine = FatigueStateMachine()

        # 推理线程控制
        self._inference_stop = Event()
        self._inference_thread: Thread | None = None

        # 信号
        self._signals = _DetectionSignals()
        self._signals.detection_ready.connect(self._on_detection_ready)
        self._signals.serial_log.connect(self._on_serial_log)
        self._signals.inference_error.connect(self._on_inference_error)

        # 串口发布器
        # CHG-G5-001: 遥测默认关闭。仅当 DMS1_EVENT_LOG 环境变量显式设置时
        # 创建 JsonlEventWriter(UTF-8 追加)作为 event_cb;关闭窗口时回收。
        self._event_writer = None
        self._event_writer_error = None
        _event_cb = None
        _log_path = os.environ.get("DMS1_EVENT_LOG")
        if _log_path:
            try:
                self._event_writer = JsonlEventWriter(_log_path)
                _event_cb = self._event_writer.write
            except OSError as exc:
                self._event_writer_error = f"Telemetry disabled: cannot open event log ({exc})"

        _metrics_path = os.environ.get("DMS1_RUNTIME_METRICS_LOG")
        if _metrics_path:
            try:
                # 仅推理线程写入此独立文件，不与串口线程共享无锁 writer。
                self._runtime_metrics_writer = JsonlEventWriter(_metrics_path)
                self._runtime_metrics_started_at = time.monotonic()
            except OSError as exc:
                self._event_writer_error = (
                    f"Runtime metrics disabled: cannot open metrics log ({exc})"
                )

        self._serial = SerialPublisher(
            port=serial_port,
            baudrate=serial_baudrate,
            heartbeat_period=SERIAL_HEARTBEAT_PERIOD,
            stale_timeout=SERIAL_RESULT_STALE_TIMEOUT,
            reconnect_initial=SERIAL_RECONNECT_INITIAL,
            reconnect_max=SERIAL_RECONNECT_MAX,
            reconnect_factor=SERIAL_RECONNECT_FACTOR,
            max_log_entries=SERIAL_MAX_LOG_ENTRIES,
            log_cb=self._signals.serial_log.emit,
            event_cb=_event_cb,
        )

        # 摄像头定时器
        self.timer_camera = QTimer()
        self.timer_camera.timeout.connect(self._show_camera)

        # UI
        self._init_ui()

        if self._event_writer_error:
            self._signals.serial_log.emit(self._event_writer_error)

        # 启动串口发布器(daemon 线程)
        self._serial.start()

        # 启动推理线程
        self._start_inference_thread()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _init_ui(self):
        """构建并布局所有界面控件。"""
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)

        # ── 上半部分：视频显示 ──
        top = QtWidgets.QHBoxLayout()
        self.label_ori_video = QtWidgets.QLabel(self)
        self.label_rec_video = QtWidgets.QLabel(self)
        for lbl in (self.label_ori_video, self.label_rec_video):
            lbl.setMinimumSize(CAMERA_WIDTH, CAMERA_HEIGHT)
            lbl.setStyleSheet('border:1px solid #D7E2F9;')
            lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.label_ori_video.setText("原始视频")
        self.label_rec_video.setText("检测结果")
        top.addWidget(self.label_ori_video)
        top.addWidget(self.label_rec_video)
        main_layout.addLayout(top)

        # ── 下半部分 ──
        bottom = QtWidgets.QHBoxLayout()

        # 运行日志
        log_group = QtWidgets.QGroupBox("运行日志")
        log_ly = QtWidgets.QVBoxLayout(log_group)
        self.textLog = QtWidgets.QTextBrowser()
        self.textLog.setMaximumHeight(120)
        self.textLog.setHtml("""
            <div style='text-align:center; font-size:16px; color:#1E90FF; font-weight:bold;'>
                Driver Fatigue Detection System
            </div>
        """)
        log_ly.addWidget(self.textLog)
        bottom.addWidget(log_group)

        # 右侧面板
        right = QtWidgets.QVBoxLayout()

        # 检测信息
        info_group = QtWidgets.QGroupBox("检测信息")
        info_ly = QtWidgets.QVBoxLayout(info_group)
        self.label_status = QtWidgets.QLabel("状态: UNKNOWN")
        self.label_status.setStyleSheet('font-size:14px; font-weight:bold;')
        self.label_count = QtWidgets.QLabel("检测: -")
        self.label_count.setStyleSheet('font-size:12px;')
        self.label_serial = QtWidgets.QLabel("串口: 启动中...")
        self.label_serial.setStyleSheet('font-size:11px; color:gray;')
        self.label_yawn_diag = QtWidgets.QLabel("YAWN武装: -- | 张嘴计时: --")
        self.label_yawn_diag.setStyleSheet('font-size:11px;')
        info_ly.addWidget(self.label_status)
        info_ly.addWidget(self.label_count)
        info_ly.addWidget(self.label_serial)
        info_ly.addWidget(self.label_yawn_diag)
        right.addWidget(info_group)

        # 控制按钮
        btn_ly = QtWidgets.QVBoxLayout()
        self.camBtn = QtWidgets.QPushButton("打开摄像头")
        self.stopBtn = QtWidgets.QPushButton("停止")
        for btn in (self.camBtn, self.stopBtn):
            btn.setMinimumHeight(40)
        self.stopBtn.setEnabled(False)
        btn_ly.addWidget(self.camBtn)
        btn_ly.addWidget(self.stopBtn)
        right.addLayout(btn_ly)

        bottom.addLayout(right)
        main_layout.addLayout(bottom)

        # 信号连接
        self.camBtn.clicked.connect(self._start_camera)
        self.stopBtn.clicked.connect(self._stop_camera)

    # ------------------------------------------------------------------
    # 推理线程
    # ------------------------------------------------------------------

    def _start_inference_thread(self):
        """启动后台推理线程。"""
        if self._inference_thread and self._inference_thread.is_alive():
            return
        self._inference_stop.clear()
        self._inference_thread = Thread(
            target=self._inference_loop, daemon=True, name="Inference"
        )
        self._inference_thread.start()

    def _inference_loop(self):
        """推理线程主循环 —— 从帧队列取帧、推理、更新状态机、发射信号。

        即使无帧也定期调用 update([]) 让状态机追踪眼部证据超时,
        在摄像头 EOF 或断线后自动过渡到 UNKNOWN。
        """
        while not self._inference_stop.is_set():
            detections = None
            frame_rgb = None

            try:
                frame_rgb = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                pass

            try:
                if frame_rgb is not None:
                    self._maybe_inject_test_inference_failure()
                    # G1-01 检测契约: detect() 返回 list[Detection]
                    detections = self.detector.detect(frame_rgb)
                    if self._test_inference_ready_at is None:
                        self._test_inference_ready_at = time.monotonic()
                else:
                    # 无帧: 传递空列表给状态机以追踪超时
                    detections = []

                # 正式状态机: FatigueStateMachine.update() 返回 FatigueResult
                result = self._state_machine.update(detections)
                yawn_snap = self._state_machine.snapshot_yawn()

                # 发布到串口
                self._serial.publish(result)

                # 仅统计真正完成 detector.detect() 的摄像头帧；无帧状态 tick 不计入 FPS。
                self._record_runtime_metrics(frame_rgb is not None)

                # 发射信号回主线程 (无帧时 frame_rgb 为 None)
                self._signals.detection_ready.emit(
                    (frame_rgb, detections, result, yawn_snap)
                )

            except Exception:
                tb = traceback.format_exc()
                self._signals.inference_error.emit(f"推理异常:\n{tb}")
                # 错误后短暂等待避免忙循环
                time.sleep(0.5)

    @staticmethod
    def _test_seconds(name: str, default: float) -> float:
        """读取受限的 E2E08 测试秒数；缺省或非法值均回退到默认值。"""
        try:
            value = float(os.environ.get(name, default))
        except ValueError:
            return default
        return value if 0.0 <= value <= 300.0 else default

    def _maybe_inject_test_inference_failure(self):
        """在首个成功推理后按需注入一次有限时长的 E2E08 故障。"""
        if self._test_inference_fault_duration_s <= 0 or self._test_inference_fault_done:
            return

        now = time.monotonic()
        ready_at = self._test_inference_ready_at
        if ready_at is None or now - ready_at < self._test_inference_fault_delay_s:
            return

        if self._test_inference_fault_until is None:
            self._test_inference_fault_until = now + self._test_inference_fault_duration_s
            self._record_test_inference_fault("start", now)
        if now < self._test_inference_fault_until:
            raise RuntimeError("Test-only injected inference failure")

        self._test_inference_fault_done = True
        self._record_test_inference_fault("recovered", now)

    def _record_test_inference_fault(self, phase: str, now: float):
        """记录 E2E08 测试事件；独立 writer 只由推理线程使用。"""
        if self._runtime_metrics_writer is not None:
            self._runtime_metrics_writer.write({
                "type": "test_inference_fault",
                "phase": phase,
                "mono_ms": int(now * 1000),
                "wall_ms": int(time.time() * 1000),
            })

    def _record_runtime_metrics(self, inference_completed: bool):
        """每五秒记录一次实际推理帧率；只在 Q-01/Q-02 显式启用。"""
        writer = self._runtime_metrics_writer
        if writer is None:
            return

        now = time.monotonic()
        if inference_completed:
            self._runtime_metrics_window_frames += 1
            self._runtime_metrics_total_frames += 1

        started_at = self._runtime_metrics_started_at
        if started_at is None or now - started_at < 5.0:
            return

        elapsed_s = now - started_at
        writer.write({
            "type": "runtime_metrics",
            "mono_ms": int(now * 1000),
            "wall_ms": int(time.time() * 1000),
            "interval_ms": int(elapsed_s * 1000),
            "inference_frames": self._runtime_metrics_window_frames,
            "inference_fps": self._runtime_metrics_window_frames / elapsed_s,
            "total_inference_frames": self._runtime_metrics_total_frames,
        })
        self._runtime_metrics_started_at = now
        self._runtime_metrics_window_frames = 0

    # ------------------------------------------------------------------
    # 主线程槽
    # ------------------------------------------------------------------

    def _on_detection_ready(self, data):
        """主线程槽: 接收推理结果并更新 UI。

        frame_rgb 为 None 时(无帧但状态机 tick)仅更新状态文本,
        不刷新检测画面。
        """
        frame_rgb, detections, result, yawn_snap = data

        # 绘制检测结果
        color_map = {
            "NORMAL": (0, 255, 0),
            "YAWN": (255, 0, 255),
            "FATIGUE": (0, 0, 255),
            "UNKNOWN": (128, 128, 128),
        }
        # result.state 是 FatigueState 枚举, .value 给出字符串
        status_text = result.state.value if hasattr(result.state, 'value') else str(result.state)
        color = color_map.get(status_text, (128, 128, 128))

        if frame_rgb is not None:
            img_rgb = self._draw_detections(frame_rgb, detections, status_text, color)
            qimg = QtGui.QImage(
                img_rgb.data, img_rgb.shape[1], img_rgb.shape[0],
                img_rgb.shape[1] * 3, QtGui.QImage.Format_RGB888
            )
            self.label_rec_video.setPixmap(QtGui.QPixmap.fromImage(qimg))

        # 更新状态文本
        detail_parts = []
        for d in detections:
            if isinstance(d, dict):
                cls = d.get("class") or d.get("class_name", "?")
                conf = d.get("confidence", 0)
            else:
                cls = getattr(d, "class_name", "?")
                conf = getattr(d, "confidence", 0)
            detail_parts.append(f"{cls}:{conf:.0%}")
        detail_text = ", ".join(detail_parts) if detail_parts else "无检测结果"
        self.label_status.setText(f"状态: {status_text}")
        self.label_status.setStyleSheet(
            f'font-size:14px; font-weight:bold; color:rgb{color};'
        )
        self.label_count.setText(f"检测: {detail_text}")

        # YAWN 诊断
        if yawn_snap.mouth_open_seconds is not None:
            timer_text = f"张嘴计时: {yawn_snap.mouth_open_seconds:.2f}/1.00 s"
        else:
            timer_text = "张嘴计时: --"
        armed_text = "是" if yawn_snap.armed else "否"
        self.label_yawn_diag.setText(f"YAWN武装: {armed_text} | {timer_text}")

    def _on_serial_log(self, msg: str):
        """主线程槽: 追加串口日志。HTML 转义防止注入。限制行数。"""
        safe = html.escape(msg)
        self.textLog.append(safe)
        # 限制 QTextBrowser 历史行数
        doc = self.textLog.document()
        if doc.blockCount() > 500:
            doc.clear()
            doc.setHtml("<div style='color:gray;'>[日志已截断]</div>")
        self.label_serial.setText(f"串口: {self._serial.port}")

    def _on_inference_error(self, msg: str):
        """主线程槽: 显示推理异常并发布 UNKNOWN。对堆栈跟踪做 HTML 转义。"""
        safe = html.escape(msg)
        self.textLog.append(f"<span style='color:red;'>{safe}</span>")
        # 使用 FatigueResult 发布 UNKNOWN
        now_ms = int(time.monotonic() * 1000)
        self._serial.publish(FatigueResult(
            state=FatigueState.UNKNOWN, evidence_score=0,
            duration_ms=0, timestamp_ms=now_ms,
        ))

    # ------------------------------------------------------------------
    # 绘制方法
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_detections(frame, detections, status, color):
        """在帧上绘制检测框、标签及状态文字。"""
        img_pil = Image.fromarray(frame)
        draw = ImageDraw.Draw(img_pil)
        try:
            font = ImageFont.truetype("simsun.ttc", 20)
            font_large = ImageFont.truetype("simsun.ttc", 28)
        except Exception:
            font = ImageFont.load_default()
            font_large = font

        for det in detections:
            if isinstance(det, dict):
                cls = det.get("class") or det.get("class_name", "?")
                conf = det.get("confidence", 0)
                bbox = det.get("bbox", (0, 0, 0, 0))
            else:
                cls = getattr(det, "class_name", "?")
                conf = getattr(det, "confidence", 0)
                bbox = getattr(det, "bbox", (0, 0, 0, 0))
            x1, y1, x2, y2 = bbox
            label = f"{cls} {conf:.2f}"
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
            draw.text((x1, y1 - 25), label, font=font, fill=color)

        draw.text((10, 10), f"状态: {status}", font=font_large, fill=color)
        return np.array(img_pil)

    # ------------------------------------------------------------------
    # 摄像头控制
    # ------------------------------------------------------------------

    def _start_camera(self):
        """打开摄像头并启动定时器。"""
        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.textLog.append("<span style='color:red;'>摄像头打开失败！</span>")
            self.cap = None
            return
        self.textLog.append("摄像头已打开，开始检测...")
        self.camBtn.setEnabled(False)
        self.stopBtn.setEnabled(True)
        self.timer_camera.start(CAMERA_TIMER_INTERVAL)

    def _show_camera(self):
        """定时器回调 —— 读取帧、显示原始画面、加入检测队列。"""
        if self.cap is None:
            return
        ret, frame = self.cap.read()
        if not ret:
            # 摄像头 EOF/断开: 不清空队列,推理线程会在陈旧超时后发送 UNKNOWN
            return

        frame = cv2.resize(frame, (CAMERA_WIDTH, CAMERA_HEIGHT))
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 显示原始视频
        qimg = QtGui.QImage(
            frame_rgb.data, frame_rgb.shape[1], frame_rgb.shape[0],
            QtGui.QImage.Format_RGB888
        )
        self.label_ori_video.setPixmap(QtGui.QPixmap.fromImage(qimg))

        # queue.Queue(maxsize=1): put_nowait 替换旧帧
        try:
            self._frame_queue.put_nowait(frame_rgb)
        except queue.Full:
            # 丢弃旧帧,放入新帧
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait(frame_rgb)
            except queue.Full:
                pass

    def _stop_camera(self):
        """停止摄像头及定时器。"""
        self.timer_camera.stop()
        # 重置状态机 (G3-01B: 替代旧的 detector.reset_fatigue_state)。
        # CPython GIL 保证对象引用赋值原子性；推理线程此时可能在旧实例上
        # 执行最后一次 update()，但新帧已停止入队，短暂交错不产生错误输出。
        self._state_machine = FatigueStateMachine()
        if self.cap:
            self.cap.release()
            self.cap = None
        # 清空帧队列
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break
        self.textLog.append("检测已停止")
        self.camBtn.setEnabled(True)
        self.stopBtn.setEnabled(False)
        self.label_ori_video.setText("原始视频")
        self.label_rec_video.setText("检测结果")

    # ------------------------------------------------------------------
    # 窗口事件
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """关闭窗口时释放所有资源: 摄像头、推理线程、串口。"""
        # 停止摄像头
        self._stop_camera()

        # 停止推理线程
        self._inference_stop.set()
        if self._inference_thread and self._inference_thread.is_alive():
            self._inference_thread.join(timeout=INFERENCE_TIMEOUT)

        # 停止串口发布器
        self._serial.stop()

        # 推理线程已退出，独立指标 writer 可以安全关闭。
        if self._runtime_metrics_writer is not None:
            self._runtime_metrics_writer.close()
            self._runtime_metrics_writer = None

        # CHG-G5-001: 回收遥测 writer(stop 之后,让 stop/disconnect 事件也能落盘)
        if self._event_writer is not None:
            self._event_writer.close()
            self._event_writer = None

        event.accept()
