"""A4 独立 G1 检测契约检查（unittest 形式，使用真实 best.pt）。

与 A1 的 handoffs/A1/G1_contract_check.py 独立实现，覆盖同一验收条款:
  - 模型可加载且类别严格等于四共享类别
  - 类别不匹配/越界编号明确报错
  - detect() 结构化 Detection 输出 + 旧 dict 兼容
  - 空画面/多分辨率不崩溃
  - 固定验证集(前 30 张)类别分布与 A1 基线一致

运行(在被测 worktree 根):
    python -m unittest test_g1_contract_a4 -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.getcwd(), "host_app"))

import cv2  # noqa: E402
import glob  # noqa: E402
import hashlib  # noqa: E402
import unittest  # noqa: E402

from detector import EXPECTED_NAMES, FatigueDetector, validate_model_names  # noqa: E402
from fatigue_state import CLASS_NAMES, Detection  # noqa: E402

MODEL = "host_app/models/best.pt"
MODEL_SHA256 = "45441dd9ff094e879fafe2b43635badb81bfc8b589e93d44a87ef3244fb19f4e"
IMAGES = sorted(glob.glob("pilao/test/images/*.jpg"))[:30]


class G1ContractA4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = FatigueDetector(MODEL)  # 加载 + 启动类别校验
        with open(MODEL, "rb") as f:
            cls.model_hash_before = hashlib.sha256(f.read()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        # 验收条款 G1-A7: 测试期间模型文件不得被修改
        with open(MODEL, "rb") as f:
            model_hash_after = hashlib.sha256(f.read()).hexdigest()
        if model_hash_after != cls.model_hash_before:
            raise AssertionError(
                f"best.pt 在测试期间被修改: 前 {cls.model_hash_before} "
                f"后 {model_hash_after}"
            )

    # -- A1: 类别严格校验 ----------------------------------------------

    def test_model_load_and_class_validation(self):
        self.assertEqual(dict(self.detector.model.names), EXPECTED_NAMES)

    # -- A7: 模型文件完整性与未被修改 ------------------------------------

    def test_model_hash_matches_baseline(self):
        """best.pt SHA-256 必须与 A1 基线记录一致（G1-A7）。"""
        self.assertEqual(
            self.model_hash_before,
            MODEL_SHA256,
            "best.pt 与 A1 基线报告 SHA-256 不一致",
        )

    def test_mismatched_classes_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_model_names(
                {0: "wrong", 1: "closed_mouth", 2: "open_eye", 3: "open_mouth"}
            )
        self.assertIn("期望", str(cm.exception))

    # -- A2: 越界类别明确错误（走产品 detect() 真实检查路径） ------------

    def test_out_of_range_class_rejected(self):
        class _FakeBox:
            cls = [99]          # 越界类别编号
            conf = [0.9]
            xyxy = [(0, 0, 10, 10)]

        class _FakeResult:
            boxes = [_FakeBox()]

        class _FakeModel:
            names = EXPECTED_NAMES

            def predict(self, source, conf=0.25, verbose=False):
                return [_FakeResult()]

        # 绕过 __init__（避免真实 YOLO 加载），桩 model.predict 返回越界
        # 类别结果，走产品 FatigueDetector.detect() 的完整解析与检查路径。
        d = FatigueDetector.__new__(FatigueDetector)
        d.model = _FakeModel()
        d.class_names = CLASS_NAMES
        with self.assertRaises(ValueError) as cm:
            d.detect(None, conf=0.25)
        self.assertIn("越界", str(cm.exception))

    # -- A3: 结构化输出与兼容性 ------------------------------------------

    def test_detection_getitem_compat(self):
        d = Detection(class_id=2, class_name="open_eye", confidence=0.9, bbox=(1, 2, 3, 4))
        self.assertEqual(d["class"], "open_eye")
        self.assertEqual(d["class_name"], "open_eye")
        self.assertEqual(d["confidence"], 0.9)
        self.assertEqual(d["bbox"], (1, 2, 3, 4))
        with self.assertRaises(KeyError):
            d["nope"]

    # -- A4: 空画面/多分辨率不崩溃 + 固定验证集基线 ----------------------

    def test_validation_set_and_robustness(self):
        self.assertGreaterEqual(len(IMAGES), 30, "固定验证集应至少 30 张")
        class_counts = {c: 0 for c in CLASS_NAMES}
        for img in IMAGES:
            frame = cv2.imread(img)
            self.assertIsNotNone(frame, f"图片可读: {img}")
            dets = self.detector.detect(frame, conf=0.25)
            self.assertTrue(all(isinstance(d, Detection) for d in dets))
            for d in dets:
                self.assertIn(d.class_name, CLASS_NAMES)
                class_counts[d.class_name] += 1
        # 与 A1 基线报告逐项一致（同一验证集、同一 conf=0.25）
        self.assertEqual(class_counts["closed_eye"], 15)
        self.assertEqual(class_counts["closed_mouth"], 0)
        self.assertEqual(class_counts["open_eye"], 40)
        self.assertEqual(class_counts["open_mouth"], 30)

    def test_empty_frame_no_crash(self):
        empty = cv2.imread(IMAGES[0]) * 0
        self.assertEqual(self.detector.detect(empty, conf=0.25), [])

    def test_multi_resolution_no_crash(self):
        frame = cv2.imread(IMAGES[0])
        for scale in (0.25, 0.5, 2.0):
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
            self.detector.detect(small, conf=0.25)  # 不崩溃即通过

    # -- A6: 预热后延迟统计（量化记录） ---------------------------------

    def test_latency_after_warmup(self):
        warmup = cv2.imread(IMAGES[0])
        self.detector.detect(warmup, conf=0.25)  # 预热（引擎初始化不计）
        latencies = []
        for img in IMAGES:
            t0 = time.perf_counter()
            self.detector.detect(img, conf=0.25)
            latencies.append(time.perf_counter() - t0)
        latencies.sort()
        n = len(latencies)
        mean_ms = sum(latencies) / n * 1000
        p50_ms = latencies[max(0, int(n * 0.50) - 1)] * 1000
        p95_ms = latencies[max(0, int(n * 0.95) - 1)] * 1000
        fps = 1.0 / (sum(latencies) / n)
        print(f"\n[延迟统计] mean={mean_ms:.1f} ms P50={p50_ms:.1f} ms "
              f"P95={p95_ms:.1f} ms 等效FPS={fps:.1f}")
        # 与 A1 基线同量级（13.0/13.2/15.0 ms, 77.1 FPS）；宽松断言防机器噪声
        self.assertLess(mean_ms, 30.0, "预热后均值延迟应 < 30ms")


if __name__ == "__main__":
    unittest.main(verbosity=2)
