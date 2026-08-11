"""G6-01 V3: 当前电脑 Windows ORT CPU 性能基线。

流程: CPUExecutionProvider 会话创建 -> 预热 30 次 -> 统计 >=300 帧(session.run 纯推理延迟,
可循环全量固定输入) -> 记录硬件/线程配置/mean/P50/P95/FPS/峰值 RSS。
验收: >=10 FPS 且 P95 <= 100 ms(项目暂定)。

用法(仓库根):
    python deployment/benchmark_ort.py --out deployment/results/benchmark_ort.json
输出: 控制台汇总 + 机读 JSON。
"""
import argparse
import json
import platform
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ONNX_PATH = ROOT / "deployment" / "models" / "best.onnx"
IMG_DIR = ROOT / "pilao" / "test" / "images"

IMGSZ = 640
WARMUP = 30
MIN_FRAMES = 300
TARGET_FPS = 10.0
TARGET_P95_MS = 100.0


def imread(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    import cv2

    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"cannot decode image: {path}")
    return img


def peak_rss_bytes():
    import psutil

    proc = psutil.Process()
    return proc.memory_info().rss  # 当前工作集;配合 run 期间轮询采样峰值


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "deployment" / "results" / "benchmark_ort.json")
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--frames", type=int, default=MIN_FRAMES)
    args = parser.parse_args()

    import onnxruntime as ort

    import onnxruntime as ort

    # 线程配置: 使用 ORT 默认(auto, 0=自动) —— 部署时的自然选择,避免人工指定造成过订阅。
    # 附: A5 实测线程扫描(intra=32/16/8/4/0, 2026-08-06)记录于 handoffs/A5 交接 §V3:
    #   intra=0(auto): mean 55.6ms / P95 57.6ms / 18.0 FPS  <-- 本基准采用
    #   intra=16:      mean 56.3ms / P95 58.2ms / 17.8 FPS
    #   intra=32:      mean 96.7ms / P95 135.5ms / 10.3 FPS (过订阅,不稳定,不采用)
    so = ort.SessionOptions()
    so.intra_op_num_threads = 0  # ORT auto
    so.inter_op_num_threads = 0  # ORT auto
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    session = ort.InferenceSession(
        str(ONNX_PATH),
        sess_options=so,
        providers=["CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name
    assert input_name == "images"

    # 固定输入:一张真实样本的 letterbox 结果(避免全零的退化路径)
    sample = sorted(IMG_DIR.glob("*.jpg"))[0]
    from ultralytics.data.augment import LetterBox

    lb = LetterBox((IMGSZ, IMGSZ), auto=False, stride=32)
    x = lb(image=imread(sample)).transpose((2, 0, 1))[::-1][None].astype(np.float32) / 255.0
    x = np.ascontiguousarray(x)

    # 预热
    for _ in range(args.warmup):
        session.run(None, {input_name: x})

    # 统计 >=frames 帧;循环同一输入以获得 >=frames 次测量
    latencies = []
    peak = 0
    n = max(args.frames, MIN_FRAMES)
    t0 = time.perf_counter()
    for i in range(n):
        s = time.perf_counter()
        session.run(None, {input_name: x})
        latencies.append((time.perf_counter() - s) * 1000.0)
        peak = max(peak, peak_rss_bytes())
    elapsed = time.perf_counter() - t0

    lat = np.array(latencies)
    mean_ms = float(lat.mean())
    p50 = float(np.percentile(lat, 50))
    p95 = float(np.percentile(lat, 95))
    fps = n / elapsed

    # 峰值内存(逐帧轮询进程工作集 RSS 的峰值;tracemalloc 与 CUDA/ort 分配口径不一致,不作为主口径)
    hardware = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "machine": platform.machine(),
        "logical_cpus": int(__import__("os").cpu_count()),
        "python": platform.python_version(),
        "onnxruntime": ort.__version__,
        "available_providers": ort.get_available_providers(),
        "session_provider": "CPUExecutionProvider",
        "intra_op_num_threads": so.intra_op_num_threads,
        "inter_op_num_threads": so.inter_op_num_threads,
        "execution_mode": "ORT_SEQUENTIAL",
    }

    result = {
        "task": "G6-01 V3 ORT CPU performance baseline (current Windows machine)",
        "input": {"name": input_name, "shape": [1, 3, IMGSZ, IMGSZ], "dtype": "float32"},
        "warmup": args.warmup,
        "frames": n,
        "latency_ms": {"mean": round(mean_ms, 4), "p50": round(p50, 4), "p95": round(p95, 4)},
        "fps": round(fps, 3),
        "elapsed_s": round(elapsed, 3),
        "peak_rss_bytes": peak,
        "acceptance": {
            "target_fps": TARGET_FPS,
            "target_p95_ms": TARGET_P95_MS,
            "fps_ok": fps >= TARGET_FPS,
            "p95_ok": p95 <= TARGET_P95_MS,
        },
        "hardware": hardware,
        "model_sha256": __import__("hashlib").sha256(Path(ONNX_PATH).read_bytes()).hexdigest(),
    }
    ok = result["acceptance"]["fps_ok"] and result["acceptance"]["p95_ok"]
    result["all_pass"] = ok

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"warmup={args.warmup} frames={n}")
    print(f"mean={mean_ms:.3f}ms  P50={p50:.3f}ms  P95={p95:.3f}ms  FPS={fps:.2f}  elapsed={elapsed:.2f}s")
    print(f"peak_rss={peak/1e6:.1f} MB")
    print(f"hardware: {hardware['platform']} / {hardware['processor']} / {hardware['logical_cpus']} cpus")
    print(f"threads: intra={so.intra_op_num_threads} inter={so.inter_op_num_threads} mode={so.execution_mode}")
    print(f"acceptance: FPS{'OK' if fps >= TARGET_FPS else 'FAIL'} ({fps:.2f} >= {TARGET_FPS})"
          f"  P95{'OK' if p95 <= TARGET_P95_MS else 'FAIL'} ({p95:.3f} <= {TARGET_P95_MS})")
    print(f"ALL_PASS={ok}")
    print(f"result written: {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
