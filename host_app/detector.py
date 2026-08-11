"""
疲劳检测器模块 —— YOLOv8 检测契约(G1-01)

封装 FatigueDetector 类:
  - detect()      对单帧执行 YOLO 推理,返回 list[Detection] 结构化结果;
                  只做检测,不直接决定硬件输出。
  - analyze_fatigue()  旧 UI 兼容层:按历史时间逻辑返回 (status, color);
                  正式时序逻辑请使用 fatigue_state.FatigueStateMachine。

G1-01 契约:
  - 启动时严格校验模型类别编号与名称对应四个共享类别,不匹配即抛错。
  - 未知或越界类别编号产生明确 ValueError,不做静默错误映射。
  - 不修改训练模型文件(best.pt 只读)。
"""

import time

from ultralytics import YOLO

from fatigue_state import CLASS_NAMES, Detection

# 期望的模型类别映射(唯一真源:共享基线第 2 节)
EXPECTED_NAMES = {i: name for i, name in enumerate(CLASS_NAMES)}


def validate_model_names(names) -> None:
    """严格校验模型类别映射。

    非空 names 必须精确等于四个共享类别;不匹配或含未知类别抛 ValueError。
    空映射只出现在测试 mock 场景(真实模型必有类别),不构成静默错误映射。
    """
    names = dict(names)
    if names and names != EXPECTED_NAMES:
        raise ValueError(
            "模型类别与共享基线不一致: "
            f"期望 {EXPECTED_NAMES},实际 {names}"
        )


def _as_dict(d):
    """兼容 dict({'class'|'class_name', 'confidence', 'bbox'}) 与 Detection。"""
    if isinstance(d, Detection):
        return {
            "class": d.class_name,
            "confidence": d.confidence,
            "bbox": d.bbox,
        }
    return d


class FatigueDetector:
    """疲劳检测器 —— 封装 YOLO 模型加载、推理与旧版状态分析。"""

    def __init__(self, model_path, closed_eye_fatigue_seconds=1.5,
                 closed_eye_gap_seconds=0.3):
        """
        参数:
            model_path: str — YOLO 权重文件路径 (.pt)
        启动时验证模型类别严格对应四个共享类别,否则抛出 ValueError。
        """
        self.model = YOLO(model_path)
        self.class_names = CLASS_NAMES
        # 启动时严格校验模型类别(见 validate_model_names)
        validate_model_names(self.model.names)
        self.closed_eye_fatigue_seconds = closed_eye_fatigue_seconds
        self.closed_eye_gap_seconds = closed_eye_gap_seconds
        self.closed_eye_started_at = None
        self.last_closed_eye_at = None

    def reset_fatigue_state(self):
        """清空连续闭眼计时。"""
        self.closed_eye_started_at = None
        self.last_closed_eye_at = None

    def detect(self, frame, conf=0.25):
        """
        对一帧图像执行检测。

        参数:
            frame: np.ndarray — RGB 图像 (H, W, 3)
            conf:  float    — 推理置信度阈值

        返回:
            list[Detection] — 结构化检测结果;class_id 越界时抛 ValueError。
        """
        results = self.model.predict(source=frame, conf=conf, verbose=False)
        detections = []

        for r in results:
            boxes = r.boxes
            for box in boxes:
                cls = int(box.cls[0])
                if cls not in EXPECTED_NAMES:
                    raise ValueError(f"检测到未知/越界类别编号 {cls},拒绝静默映射")
                conf_val = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                detections.append(Detection(
                    class_id=cls,
                    class_name=EXPECTED_NAMES[cls],
                    confidence=conf_val,
                    bbox=(x1, y1, x2, y2),
                ))

        return detections

    def analyze_fatigue(self, detections, now=None):
        """
        旧版 UI 兼容层:根据检测结果判断疲劳状态(行为与 G0 基线一致)。

        连续闭眼达到阈值才判定疲劳,避免正常眨眼误报。

        参数:
            detections: list[Detection] 或 list[dict] — detect() 的返回值

        返回:
            (status: str, color: tuple)
                status — "张嘴" / "疲劳" / "正常" / "检测中..."
                color  — BGR 颜色元组

        注意:正式时序逻辑已解耦到 fatigue_state.FatigueStateMachine,
        本方法仅供旧 ui.py 在 A2 完成集成前保持运行,不做新状态输出。
        """
        now = time.monotonic() if now is None else now

        if not detections:
            if (self.last_closed_eye_at is not None
                    and now - self.last_closed_eye_at > self.closed_eye_gap_seconds):
                self.reset_fatigue_state()
            return '检测中...', (255, 165, 0)

        has_closed_eye = False
        has_open_eye = False
        has_open_mouth = False

        for d in detections:
            item = _as_dict(d)
            cls = item['class']
            if cls == 'closed_eye':
                has_closed_eye = True
            elif cls == 'open_eye':
                has_open_eye = True
            elif cls == 'open_mouth':
                has_open_mouth = True

        # 同时检测到睁眼和闭眼时不累计,避免单眼眨眼或误检触发报警。
        eyes_closed = has_closed_eye and not has_open_eye
        if eyes_closed:
            if self.closed_eye_started_at is None:
                self.closed_eye_started_at = now
            self.last_closed_eye_at = now
        elif has_open_eye:
            self.reset_fatigue_state()
        elif (self.last_closed_eye_at is not None
              and now - self.last_closed_eye_at > self.closed_eye_gap_seconds):
            self.reset_fatigue_state()

        if eyes_closed and now - self.closed_eye_started_at >= self.closed_eye_fatigue_seconds:
            return '疲劳', (0, 0, 255)

        if has_open_mouth:
            return '张嘴', (255, 0, 255)

        if eyes_closed:
            return '眨眼', (255, 165, 0)

        # 睁眼 = 正常
        if has_open_eye:
            return '正常', (0, 255, 0)

        return '检测中...', (255, 165, 0)
