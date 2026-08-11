"""G6-01 PT/ONNX 模型等价性验收测试(标准库 unittest)。

覆盖:
- 模型可加载(PT + ONNX Runtime session + onnx.checker)
- 类别契约严格一致(共享四类别,与 manifest 绑定)
- ONNX 图输入/输出 shape/类型/opset
- manifest 哈希与实际文件一致
- 固定样本清单(独立文件 samples.json + 每样本 SHA-256)哈希与实际文件一致
- 固定样本 PT/ONNX 原始输出数值等价
- 固定样本 PT/ONNX 检测结果(解码+NMS 后)集合等价
- 四类检出并集显式断言: 清单样本(PT 侧)检出类别并集 == {closed_eye, closed_mouth, open_eye, open_mouth}
- 空画面(全零输入)PT/ONNX 均不崩溃(显式断言)
- 不同原始分辨率样本(1280x960)PT/ONNX 均不崩溃且结果一致

运行(工作目录: 仓库根):
    python -m unittest tests.test_model_equivalence -v
不依赖摄像头,只读 pilao/test/images 与 deployment/models/samples 下的固定样本。
"""
import hashlib
import json
import unittest
from pathlib import Path

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
SRC_PT = ROOT / "host_app" / "models" / "best.pt"
ONNX_PATH = ROOT / "deployment" / "models" / "best.onnx"
MANIFEST_PATH = ROOT / "deployment" / "models" / "manifest.json"
SAMPLES_PATH = ROOT / "deployment" / "models" / "samples.json"

CLASSES = ["closed_eye", "closed_mouth", "open_eye", "open_mouth"]
CLASS_INDEX = dict(zip(CLASSES, range(len(CLASSES))))
IMGSZ = 640
OPSET_EXPECT = 12
CONF_THRES = 0.25
IOU_THRES = 0.45
RAW_TOL = 1e-3  # PT 与 ORT 原始输出最大绝对差上界(实测峰值 8.09e-4)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def imread(path):
    """cv2.imread 在 CJK 路径下偶发失败,统一走 bytes+imdecode。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"cannot decode image: {path}")
    return img


class PTONNXEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ultralytics import YOLO
        from ultralytics.data.augment import LetterBox

        import onnxruntime as ort
        import onnx

        cls.onnx = onnx
        cls.pt = YOLO(str(SRC_PT))
        cls.session = ort.InferenceSession(str(ONNX_PATH), providers=["CPUExecutionProvider"])
        cls.input_name = cls.session.get_inputs()[0].name
        cls.letterbox = LetterBox((IMGSZ, IMGSZ), auto=False, stride=32)
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.samples_manifest = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
        cls.onnx_model = onnx.load(str(ONNX_PATH))

    # -- 辅助 -----------------------------------------------------------
    def _preprocess(self, img):
        boxed = self.letterbox(image=img)
        x = boxed.transpose((2, 0, 1))[::-1][None].astype(np.float32) / 255.0  # BGR->RGB, /255, NCHW
        return torch.from_numpy(x.copy()), np.ascontiguousarray(x)

    def _run_pt_raw(self, x):
        with torch.no_grad():
            out = self.pt.model(x)
        return out[0] if isinstance(out, (list, tuple)) else out

    def _run_ort_raw(self, x):
        return torch.from_numpy(self.session.run(None, {self.input_name: x})[0])

    def _det_set(self, raw):
        from ultralytics.utils.ops import non_max_suppression

        r = non_max_suppression(raw, conf_thres=CONF_THRES, iou_thres=IOU_THRES)[0]
        if r is None or len(r) == 0:
            return set()
        return {
            (int(x[5]), round(float(x[4]), 4), round(float(x[0]), 1),
             round(float(x[1]), 1), round(float(x[2]), 1), round(float(x[3]), 1))
            for x in r
        }

    def _det_classes(self, raw):
        from ultralytics.utils.ops import non_max_suppression

        r = non_max_suppression(raw, conf_thres=CONF_THRES, iou_thres=IOU_THRES)[0]
        if r is None or len(r) == 0:
            return set()
        return set(int(x[5]) for x in r)

    # -- 基础契约 -------------------------------------------------------
    def test_model_files_loadable(self):
        self.assertTrue(SRC_PT.exists())
        self.assertTrue(ONNX_PATH.exists())
        self.assertEqual(self.pt.task, "detect")
        self.assertIsNotNone(self.session.run(None, {self.input_name: np.zeros((1, 3, IMGSZ, IMGSZ), np.float32)}))
        self.onnx.checker.check_model(self.onnx_model)  # 图结构合法

    def test_category_contract_strict(self):
        self.assertEqual(list(dict(self.pt.names).values()), CLASSES)
        self.assertEqual(self.manifest["classes"], CLASSES)
        self.assertEqual(sorted(self.manifest["classes"]), sorted(CLASSES))

    def test_onnx_graph_io(self):
        g = self.onnx_model.graph
        self.assertEqual([o.version for o in self.onnx_model.opset_import], [OPSET_EXPECT])
        (inp,) = g.input
        (out,) = g.output
        in_shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        out_shape = [d.dim_value for d in out.type.tensor_type.shape.dim]
        self.assertEqual(in_shape, [1, 3, IMGSZ, IMGSZ])
        self.assertEqual(out_shape, [1, 8, 8400])
        self.assertEqual(inp.type.tensor_type.elem_type, 1)   # float32
        self.assertEqual(out.type.tensor_type.elem_type, 1)

    def test_manifest_hashes_match_files(self):
        self.assertEqual(self.manifest["source_sha256"], sha256(SRC_PT))
        self.assertEqual(self.manifest["target_sha256"], sha256(ONNX_PATH))

    def test_samples_manifest_hash_bound_to_models_manifest(self):
        """清单文件自身 SHA-256 必须在 models/manifest.json 中记录(REV-G6-006)。"""
        self.assertEqual(
            self.manifest["validation_samples"]["manifest_sha256"],
            sha256(SAMPLES_PATH),
            "samples.json SHA-256 与 models/manifest.json 记录不一致",
        )
        self.assertEqual(
            self.manifest["validation_samples"]["count"],
            self.samples_manifest["count"],
        )

    # -- 固定样本清单独立文件 + 每样本哈希 --------------------------------
    def test_samples_manifest_self_consistent(self):
        samples = self.samples_manifest["samples"]
        self.assertGreaterEqual(len(samples), 1, "固定样本清单为空")
        self.assertEqual(self.samples_manifest["count"], len(samples))
        self.assertEqual(
            self.samples_manifest["nms"],
            {"conf_thres": CONF_THRES, "iou_thres": IOU_THRES, "max_det": 300},
        )
        files = set()
        for s in samples:
            p = ROOT / s["dir"] / s["file"]
            self.assertTrue(p.exists(), f"清单样本缺失: {p}")
            self.assertEqual(s["sha256"], sha256(p), f"清单 SHA-256 与文件不符: {p}")
            self.assertNotIn(s["file"], files, f"清单样本重复: {s['file']}")
            files.add(s["file"])
        # 至少一个不同原始分辨率样本
        self.assertTrue(
            any(s.get("note") for s in samples),
            "固定样本清单中无不同原始分辨率样本",
        )

    # -- PT/ONNX 原始输出与检测集合等价(遍历清单全部样本) ------------------
    def test_raw_output_equivalence(self):
        for s in self.samples_manifest["samples"]:
            p = ROOT / s["dir"] / s["file"]
            x_t, x_n = self._preprocess(imread(p))
            diff = float((self._run_pt_raw(x_t) - self._run_ort_raw(x_n)).abs().max())
            self.assertLessEqual(diff, RAW_TOL, f"{s['file']}: PT/ONNX 原始输出最大差 {diff:.3e} 超上界 {RAW_TOL}")

    def test_detection_equivalence(self):
        for s in self.samples_manifest["samples"]:
            p = ROOT / s["dir"] / s["file"]
            x_t, x_n = self._preprocess(imread(p))
            pt_set = self._det_set(self._run_pt_raw(x_t))
            ort_set = self._det_set(self._run_ort_raw(x_n))
            self.assertEqual(pt_set, ort_set, f"{s['file']}: PT/ONNX 检测结果集合不一致")
            self.assertGreater(len(pt_set), 0, f"{s['file']}: 无任何检测,样本清单失效")

    # -- 四类覆盖显式断言(REV-G6-001) -------------------------------------
    def test_four_class_union_covered(self):
        """清单样本(PT 侧, conf=0.25)检出类别并集必须 == 全部四类。"""
        union = set()
        per_class_hit = {i: [] for i in range(4)}
        for s in self.samples_manifest["samples"]:
            p = ROOT / s["dir"] / s["file"]
            x_t, _ = self._preprocess(imread(p))
            classes = self._det_classes(self._run_pt_raw(x_t))
            union |= classes
            for c in classes:
                per_class_hit[c].append(s["file"])
        self.assertEqual(union, set(range(4)),
                         f"清单检出并集 {union} != 全部四类 {set(range(4))}")
        for c, hits in per_class_hit.items():
            self.assertGreater(len(hits), 0,
                               f"类别 {CLASSES[c]}(idx {c}) 无任何检出样本")

    def test_open_mouth_sample_covered(self):
        """关键类 open_mouth(3) 必须在固定清单中有实际检出样本。"""
        covered = any(
            self._det_classes(self._run_pt_raw(self._preprocess(
                imread(ROOT / s["dir"] / s["file"]))[0])) >= {3}
            for s in self.samples_manifest["samples"]
        )
        self.assertTrue(covered, "固定样本清单中没有任何样本检出 open_mouth")

    # -- 空画面(全零输入)显式断言(REV-G6-004) ------------------------------
    def test_empty_zero_image_no_crash(self):
        """全零输入(空画面代理) PT 与 ONNX 均必须不崩溃且输出有限。"""
        x = torch.zeros((1, 3, IMGSZ, IMGSZ), dtype=torch.float32)
        x_n = np.zeros((1, 3, IMGSZ, IMGSZ), dtype=np.float32)
        pt_out = self._run_pt_raw(x)
        ort_out = self._run_ort_raw(x_n)
        self.assertTrue(torch.isfinite(pt_out).all(), "PT 空画面输出含非有限值")
        self.assertTrue(torch.isfinite(ort_out).all(), "ONNX 空画面输出含非有限值")
        self.assertEqual(self._det_classes(pt_out), self._det_classes(ort_out),
                         "空画面 PT/ONNX 检测结果应一致(通常均为空)")

    # -- 不同原始分辨率样本(REV-G6-004) ------------------------------------
    def test_different_resolution_sample_no_crash(self):
        """清单中不同原始分辨率样本(如 1280x960) PT/ONNX 均不崩溃且结果一致。"""
        for s in self.samples_manifest["samples"]:
            if not s.get("note"):
                continue
            p = ROOT / s["dir"] / s["file"]
            img = imread(p)
            self.assertNotEqual(tuple(img.shape[:2]), (IMGSZ, IMGSZ),
                                f"样本 {s['file']} 原始分辨率应异于 640x640")
            x_t, x_n = self._preprocess(img)
            self.assertEqual(self._det_set(self._run_pt_raw(x_t)),
                             self._det_set(self._run_ort_raw(x_n)),
                             f"{s['file']}: 不同分辨率下 PT/ONNX 检测结果不一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
