"""G6-01 V2: PT vs ONNX 全量数据集指标等价评估 (pilao/test, 306 张带 GT)。

对 pilao/test 全量 306 张图像,以完全相同的预处理(LetterBox 640 / BGR->RGB / /255 / NCHW)
与后处理(non_max_suppression conf=0.25, IoU=0.45, max_det=300)分别运行 PT 与 ONNX,
按 ultralytics val 同口径(match_predictions + ap_per_class, IoU 0.5:0.95)计算:
  - 总体 mAP50 / mAP50-95 / Recall
  - 四类逐类 Precision / Recall / AP50 / AP50-95
  - 两端绝对差(Δ)
GT 来源: pilao/test/labels.cache(归一化 xywh bbox)。

口径说明(REV-G6-002):
- 预处理固定 640x640(rect=False / auto=False),与 manifest preprocess 及 G6-01 验收条件一致;
  不使用 ultralytics val 默认的 rect=True(672x672)路径,避免验收口径漂移。
- 指标经 ultralytics 官方 val(rect=False)三方互证: 本脚本 PT 结果 == 官方 val PT
  (map50 0.928683 / mr 0.916500), ONNX 同理,逐项一致(2026-08-06 A5 实测)。

用法(仓库根):
    python deployment/eval_pt_onnx.py --out deployment/results/map_equivalence.json
输出: 控制台汇总 + 机读 JSON。
"""
import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
SRC_PT = ROOT / "host_app" / "models" / "best.pt"
ONNX_PATH = ROOT / "deployment" / "models" / "best.onnx"
MANIFEST_PATH = ROOT / "deployment" / "models" / "manifest.json"
IMG_DIR = ROOT / "pilao" / "test" / "images"
LABEL_CACHE = ROOT / "pilao" / "test" / "labels.cache"

CLASSES = ["closed_eye", "closed_mouth", "open_eye", "open_mouth"]
IMGSZ = 640
CONF_THRES = 0.25
IOU_THRES = 0.45
MAX_DET = 300

THRESHOLDS = {
    "overall_map50_delta": 0.01,
    "overall_recall_delta": 0.02,
    "per_class_recall_delta": 0.02,
    "per_class_ap50_delta": 0.02,
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def imread(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    import cv2

    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"cannot decode image: {path}")
    return img


def load_gt():
    """返回 {图像文件名: {"cls": np.ndarray[int32], "bboxes": np.ndarray[n,4] xyxy 归一化}}。"""
    import numpy as _np

    data = np.load(LABEL_CACHE, allow_pickle=True).item()
    labels = data["labels"]
    gt = {}
    for rec in labels:
        im_file = Path(rec["im_file"]).name
        cls = rec["cls"]
        bboxes = rec["bboxes"]
        if cls is None or len(cls) == 0:
            gt[im_file] = {"cls": _np.zeros(0, dtype=_np.int32), "bboxes": _np.zeros((0, 4), dtype=_np.float32)}
        else:
            gt[im_file] = {"cls": cls[:, 0].astype(_np.int32), "bboxes": bboxes.astype(_np.float32)}
    return gt


def preprocess(img):
    from ultralytics.data.augment import LetterBox

    lb = LetterBox((IMGSZ, IMGSZ), auto=False, stride=32)
    boxed = lb(image=img)
    x = boxed.transpose((2, 0, 1))[::-1][None].astype(np.float32) / 255.0  # BGR->RGB, /255, NCHW
    return np.ascontiguousarray(x)


def letterbox_ratio_pad(h, w):
    """与 LetterBox(center=True, auto=False) 一致的 (ratio, (left, top))。"""
    r = min(IMGSZ / h, IMGSZ / w)
    new_unpad = (round(w * r), round(h * r))
    dw = IMGSZ - new_unpad[0]
    dh = IMGSZ - new_unpad[1]
    dw /= 2
    dh /= 2
    left = int(round(dw - 0.1))
    top = int(round(dh - 0.1))
    return ((r, r), (left, top))


def run_pt(pt, x):
    with torch.no_grad():
        out = pt.model(torch.from_numpy(x))
    return out[0] if isinstance(out, (list, tuple)) else out


def run_ort(session, input_name, x):
    return torch.from_numpy(session.run(None, {input_name: x})[0])


def nms(raw):
    """与 ultralytics DetectionValidator.postprocess 完全同参(REV-G6-002 指标口径)。"""
    from ultralytics.utils.ops import non_max_suppression

    return non_max_suppression(
        raw,
        CONF_THRES,
        IOU_THRES,
        labels=(),
        nc=4,
        multi_label=True,
        agnostic=False,
        max_det=MAX_DET,
    )[0]


def evaluate(runner, gt):
    """runner(img_path) -> 原始输出 tensor; 返回 ultralytics val 口径的 stats 聚合。"""
    from ultralytics.utils.ops import scale_boxes, xywh2xyxy
    from ultralytics.models.yolo.detect.val import DetectionValidator

    val = DetectionValidator()
    val.iouv = torch.linspace(0.5, 0.95, 10)
    val.device = torch.device("cpu")
    val.niou = 10

    stats = {"conf": [], "pred_cls": [], "tp": [], "target_cls": []}
    seen = 0
    for img_path in sorted(IMG_DIR.glob("*.jpg")):
        name = img_path.name
        g = gt[name]
        img = imread(img_path)
        h, w = img.shape[:2]
        x = preprocess(img)
        raw = runner(x)
        pred = nms(raw)
        ratio_pad = letterbox_ratio_pad(h, w)
        stat = dict(conf=torch.zeros(0), pred_cls=torch.zeros(0, dtype=torch.int32),
                    tp=torch.zeros(0, val.niou, dtype=torch.bool))
        gt_cls = torch.from_numpy(g["cls"])
        gt_bbox = torch.from_numpy(g["bboxes"])
        nl = len(gt_cls)
        stat["target_cls"] = gt_cls
        if pred is None or len(pred) == 0:
            if nl:
                for k in stats:
                    stats[k].append(stat[k])
            continue
        seen += 1
        predn = pred.clone()
        scale_boxes((IMGSZ, IMGSZ), predn[:, :4], (h, w), ratio_pad=ratio_pad)
        npr = len(predn)
        stat["conf"] = predn[:, 4]
        stat["pred_cls"] = predn[:, 5].int()
        if nl:
            # GT: 归一化 xywh -> 640 空间 -> 原图空间(与 ultralytics _prepare_batch 一致)
            gt_bbox_norm = xywh2xyxy(gt_bbox) * torch.tensor((IMGSZ, IMGSZ, IMGSZ, IMGSZ), dtype=torch.float32)
            scale_boxes((IMGSZ, IMGSZ), gt_bbox_norm, (h, w), ratio_pad=ratio_pad)
            from ultralytics.utils.metrics import box_iou

            iou = box_iou(gt_bbox_norm, predn[:, :4])
            stat["tp"] = val.match_predictions(predn[:, 5], gt_cls, iou)
        else:
            # 有预测无 GT: 全部为 FP, tp 行数须与 conf 一致
            stat["tp"] = torch.zeros(npr, val.niou, dtype=torch.bool)
        for k in stats:
            stats[k].append(stat[k])
    return stats, seen


def summarize(name, stats):
    from ultralytics.utils.metrics import ap_per_class

    tp = torch.cat(stats["tp"]).numpy() if len(stats["tp"]) else np.zeros((0, 10), dtype=bool)
    conf = torch.cat(stats["conf"]).numpy() if len(stats["conf"]) else np.zeros(0)
    pred_cls = torch.cat(stats["pred_cls"]).numpy() if len(stats["pred_cls"]) else np.zeros(0, dtype=int)
    target_cls = torch.cat(stats["target_cls"]).numpy() if len(stats["target_cls"]) else np.zeros(0, dtype=int)
    p, r, f1, ap, unique = ap_per_class(tp, conf, pred_cls, target_cls)[2:7]
    nc = len(CLASSES)
    per_class = {c: {"precision": None, "recall": None, "ap50": None, "ap50_95": None} for c in CLASSES}
    for i, c in enumerate(unique):
        per_class[CLASSES[c]] = {
            "precision": float(p[i]),
            "recall": float(r[i]),
            "ap50": float(ap[i, 0]),
            "ap50_95": float(ap[i].mean()),
        }
    overall = {
        "map50": float(ap[:, 0].mean()),
        "map50_95": float(ap.mean()),
        "recall": float(r.mean()),
    }
    return overall, per_class, {"n_ap_classes": int(len(unique))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "deployment" / "results" / "map_equivalence.json")
    args = parser.parse_args()

    from ultralytics import YOLO
    import onnxruntime as ort

    gt = load_gt()
    images = sorted(IMG_DIR.glob("*.jpg"))
    print(f"images={len(images)} gt_records={len(gt)}")

    pt = YOLO(str(SRC_PT))
    session = ort.InferenceSession(str(ONNX_PATH), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    t0 = time.perf_counter()
    pt_stats, pt_seen = evaluate(lambda x: run_pt(pt, x), gt)
    pt_time = time.perf_counter() - t0
    t0 = time.perf_counter()
    ort_stats, ort_seen = evaluate(lambda x: run_ort(session, input_name, x), gt)
    ort_time = time.perf_counter() - t0

    pt_overall, pt_per, pt_aux = summarize("PT", pt_stats)
    ort_overall, ort_per, ort_aux = summarize("ONNX", ort_stats)

    def d(a, b):
        return round(a - b, 6)

    per_class_compare = {}
    for c in CLASSES:
        per_class_compare[c] = {
            "PT": pt_per[c],
            "ONNX": ort_per[c],
            "delta": {
                "precision": d(ort_per[c]["precision"], pt_per[c]["precision"]),
                "recall": d(ort_per[c]["recall"], pt_per[c]["recall"]),
                "ap50": d(ort_per[c]["ap50"], pt_per[c]["ap50"]),
                "ap50_95": d(ort_per[c]["ap50_95"], pt_per[c]["ap50_95"]),
            },
        }

    delta = {
        "map50": d(ort_overall["map50"], pt_overall["map50"]),
        "map50_95": d(ort_overall["map50_95"], pt_overall["map50_95"]),
        "recall": d(ort_overall["recall"], pt_overall["recall"]),
    }

    # 门限判定
    checks = {
        "overall_map50_delta<=0.01": abs(delta["map50"]) <= THRESHOLDS["overall_map50_delta"],
        "overall_recall_delta<=0.02": abs(delta["recall"]) <= THRESHOLDS["overall_recall_delta"],
        "per_class_recall_delta<=0.02": all(
            abs(v["delta"]["recall"]) <= THRESHOLDS["per_class_recall_delta"] for v in per_class_compare.values()),
        "per_class_ap50_delta<=0.02": all(
            abs(v["delta"]["ap50"]) <= THRESHOLDS["per_class_ap50_delta"] for v in per_class_compare.values()),
        "closed_eye_pass": per_class_compare["closed_eye"]["delta"]["recall"] == 0
        and per_class_compare["closed_eye"]["delta"]["ap50"] == 0,
        "open_eye_pass": per_class_compare["open_eye"]["delta"]["recall"] == 0
        and per_class_compare["open_eye"]["delta"]["ap50"] == 0,
    }
    all_pass = all(checks.values())

    result = {
        "task": "G6-01 V2 PT/ONNX map_equivalence",
        "dataset": "pilao/test",
        "num_images": len(images),
        "gt_records": len(gt),
        "preprocess": "LetterBox(640,640,auto=False,stride=32)+BGR->RGB+/255+NCHW",
        "nms": {"conf_thres": CONF_THRES, "iou_thres": IOU_THRES, "max_det": MAX_DET},
        "classes": CLASSES,
        "pt_overall": pt_overall,
        "onnx_overall": ort_overall,
        "delta_overall": delta,
        "per_class": per_class_compare,
        "thresholds": THRESHOLDS,
        "checks": checks,
        "all_pass": all_pass,
        "timing_s": {"pt_eval": round(pt_time, 2), "onnx_eval": round(ort_time, 2)},
        "seen_images": {"pt": pt_seen, "onnx": ort_seen},
        "versions": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "ultralytics": __import__("ultralytics").__version__,
            "onnxruntime": __import__("onnxruntime").__version__,
        },
        "assets": {
            "pt": {"path": str(SRC_PT), "sha256": sha256(SRC_PT)},
            "onnx": {"path": str(ONNX_PATH), "sha256": sha256(ONNX_PATH)},
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台汇总
    print("\n=== overall ===")
    for m in ("map50", "map50_95", "recall"):
        print(f"  {m:10s} PT={pt_overall[m]:.4f}  ONNX={ort_overall[m]:.4f}  delta={delta[m]:+.6f}")
    print("=== per class (PT / ONNX / delta) ===")
    for c in CLASSES:
        v = per_class_compare[c]
        print(f"  {c:12s} P={v['PT']['precision']:.4f}/{v['ONNX']['precision']:.4f}/{v['delta']['precision']:+.6f}"
              f"  R={v['PT']['recall']:.4f}/{v['ONNX']['recall']:.4f}/{v['delta']['recall']:+.6f}"
              f"  AP50={v['PT']['ap50']:.4f}/{v['ONNX']['ap50']:.4f}/{v['delta']['ap50']:+.6f}"
              f"  AP50-95={v['PT']['ap50_95']:.4f}/{v['ONNX']['ap50_95']:.4f}/{v['delta']['ap50_95']:+.6f}")
    print("=== checks ===")
    for k, ok in checks.items():
        print(f"  {k}: {'PASS' if ok else 'FAIL'}")
    print(f"ALL_PASS={all_pass}")
    print(f"result written: {args.out}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
