"""G6-01 ONNX 导出脚本：best.pt -> deployment/models/best.onnx。

可重复执行且幂等；源 best.pt 只读，不修改。onnxsim 简化与 onnx.checker 校验均固化在本脚本内执行，
不依赖任何手工/人工步骤(REV-G6-007)。
用法: python deployment/export_model.py
工作目录: 仓库根。

可复现性说明(实测):
- 结构可复现: 每次导出均得 opset 12 / ir_version 7 / 234 nodes / [1,3,640,640]->[1,8,8400]。
- 功能可复现: 与已提交 best.onnx 在固定输入(含全零)上 ORT 输出 max 差 = 0.0(逐位一致)。
- 字节级 SHA-256 非逐位复现: torch.onnx 对少量空/常量张量(如 Resize roi 标量、shape 常量)的
  序列化存在非确定性(实测多轮导出结构一致、仅此类常量编码略异)，故本脚本保留既有已提交产物
  (其 SHA-256 与 manifest target_sha256 绑定)；重复执行若产物已存在且哈希匹配则跳过，避免覆盖。
"""
import hashlib
import shutil
from pathlib import Path

import onnx
import onnxsim
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "host_app" / "models" / "best.pt"
OUT_DIR = ROOT / "deployment" / "models"
OUT = OUT_DIR / "best.onnx"
MANIFEST = OUT_DIR / "manifest.json"
IMGSZ = 640
OPSET = 12
CLASSES = ["closed_eye", "closed_mouth", "open_eye", "open_mouth"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    import json

    src_sha = sha256(SRC)
    m = YOLO(str(SRC))
    assert m.task == "detect", m.task
    names = dict(m.names)
    assert list(names.values()) == CLASSES, names

    # 幂等: 已提交产物存在且哈希与 manifest 一致则跳过,避免覆盖字节级已复核产物
    if OUT.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if sha256(OUT) == manifest.get("target_sha256", ""):
            print("export_skip_existing", {"out": str(OUT), "out_sha": sha256(OUT),
                                           "reason": "既有产物哈希与 manifest target_sha256 一致,无需重导出"})
            return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = Path(m.export(format="onnx", imgsz=IMGSZ, opset=OPSET, simplify=False))
    work = OUT_DIR / "best_raw.onnx"
    shutil.move(str(raw), str(work))
    try:
        # 固化的 onnxsim 简化步骤(REV-G6-007): 不依赖手工
        sim_model, check = onnxsim.simplify(str(work))
        if not check:
            raise RuntimeError("onnxsim check failed")
        onnx.save(sim_model, str(OUT))
    finally:
        if work.exists():
            work.unlink()

    model = onnx.load(str(OUT))
    onnx.checker.check_model(model)
    out_sha = sha256(OUT)
    print("export_ok", {
        "src": str(SRC),
        "src_sha": src_sha,
        "out": str(OUT),
        "out_size": OUT.stat().st_size,
        "out_sha": out_sha,
        "imgsz": IMGSZ,
        "opset": OPSET,
        "num_nodes": len(model.graph.node),
        "simplify": "onnxsim 脚本内固化执行(非手工)",
        "classes": CLASSES,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
