"""A4 独立扩展测试：G1/G2 门禁被测版本 d548f8b 的人工时钟/帧率/边界/冲突/负面验证。

与 A1 的 tests/test_fatigue_state.py 独立构造（不同序列编排、独立边界扫描），
用于交叉验证状态机实现。只读被测模块，不修改任何产品代码。

运行（在被测 worktree 根）:
    python <本文件绝对路径>
退出码: 0=全部 PASS, 1=存在 FAIL。

覆盖范围（A4 角色文件 A1 状态机测试要求）:
  - 人工时钟 10/20/30 FPS 不改变时间阈值
  - 阈值边界: 1.49/1.50、0.29/0.30/0.31、0.49/0.50、0.99/1.00
  - 空检测、多目标、低置信度、未知类别负面测试
  - 眼/嘴相反类别冲突、初始闭眼、YAWN 最小保持与闭嘴退出组合边界
  - 先 YAWN 后 FATIGUE 且不回放、FATIGUE 证据丢失、配置校验
"""
import os
import sys
import tempfile
import unittest

# 定位被测模块：本文件可能位于 <root>/tests/integration/ 或直接被复制到
# <root> 下运行；向上查找包含 configs/fatigue.yaml 的目录作为仓库根。
_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root(start):
    cur = start
    while True:
        if os.path.isfile(os.path.join(cur, "configs", "fatigue.yaml")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RuntimeError(f"找不到仓库根(含 configs/fatigue.yaml): {start}")
        cur = parent


_ROOT = _find_root(_HERE)
_PRODUCT = os.path.join(_ROOT, "host_app")
sys.path.insert(0, _PRODUCT)

import yaml  # noqa: E402

from fatigue_state import (  # noqa: E402
    CLASS_NAMES,
    DEFAULT_CONFIG_PATH,
    Detection,
    FatigueState,
    FatigueStateMachine,
    load_fatigue_config,
)

CFG = load_fatigue_config(DEFAULT_CONFIG_PATH)


def det(name, conf=0.90):
    return Detection(
        class_id=CLASS_NAMES.index(name),
        class_name=name,
        confidence=conf,
        bbox=(0, 0, 10, 10),
    )


class A4ExtendedStateMachineTest(unittest.TestCase):
    # -- E-01: 帧率无关性（人工时钟，10/20/30 FPS） ---------------------

    def test_e01_fps_independent_full_scenario(self):
        """同一完整事件序列以 10/20/30 FPS 回放，状态切换时刻误差 <= 1 采样周期。"""
        for fps in (10, 20, 30):
            dt = 1.0 / fps
            t = 1000.0
            sm = FatigueStateMachine(CFG)
            # 阶段1: 睁眼 -> NORMAL
            r = sm.update([det("open_eye"), det("closed_mouth")], t)
            self.assertEqual(r.state, FatigueState.NORMAL, f"fps={fps}")
            # 阶段2: 闭眼 -> FATIGUE(1.5s)
            t += dt
            sm.update([det("closed_eye"), det("closed_mouth")], t)
            closed_start = t
            while True:
                t += dt
                r = sm.update([det("closed_eye"), det("closed_mouth")], t)
                if r.state == FatigueState.FATIGUE:
                    break
            self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6, f"fps={fps}")
            # 阶段3: 睁眼恢复 -> NORMAL(0.5s)
            t += dt
            sm.update([det("open_eye"), det("closed_mouth")], t)
            rec_start = t
            while True:
                t += dt
                r = sm.update([det("open_eye"), det("closed_mouth")], t)
                if r.state == FatigueState.NORMAL:
                    break
            self.assertLessEqual(abs(t - (rec_start + 0.5)), dt + 1e-6, f"fps={fps}")
            # 阶段4: 张嘴 -> YAWN(1.0s)
            t += dt
            sm.update([det("open_eye"), det("open_mouth")], t)
            mouth_start = t
            while True:
                t += dt
                r = sm.update([det("open_eye"), det("open_mouth")], t)
                if r.state == FatigueState.YAWN:
                    break
            self.assertLessEqual(abs(t - (mouth_start + 1.0)), dt + 1e-6, f"fps={fps}")

    # -- E-02: 闭眼阈值边界 1.49 / 1.50 ---------------------------------

    def test_e02_closed_eye_boundary(self):
        sm = FatigueStateMachine(CFG)
        t = 200.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)   # 闭眼起点 201.0
        r = sm.update([det("closed_eye")], t + 2.49)  # 1.49s: 不触发
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([det("closed_eye")], t + 2.50)  # 1.50s: 触发
        self.assertEqual(r.state, FatigueState.FATIGUE)

    # -- E-03: 眼部漏检宽限边界 0.50 / 0.51 -----------------------------

    def test_e03_eye_missing_grace_boundary(self):
        for gap, expect_keep in ((0.50, True), (0.51, False)):
            # 闭眼起点 t+1.0，闭眼 1.0s 后漏检 gap，再恢复闭眼 0.5s:
            #   起点保留(0.50): 总闭眼 1.5s+gap 超过阈值 -> FATIGUE
            #   起点清零(0.51): 新计时仅 0.5s -> 非 FATIGUE
            sm = FatigueStateMachine(CFG)
            t = 200.0
            sm.update([det("open_eye")], t)
            sm.update([det("closed_eye")], t + 1.0)
            sm.update([det("closed_eye")], t + 2.0)
            sm.update([], t + 2.0 + gap)                 # 漏检 gap
            r = sm.update([det("closed_eye")], t + 2.0 + gap + 0.5)  # 恢复闭眼 0.5s
            if expect_keep:
                self.assertEqual(r.state, FatigueState.FATIGUE, f"gap={gap}")
            else:
                self.assertNotEqual(r.state, FatigueState.FATIGUE, f"gap={gap}")

    # -- E-04: 疲劳恢复边界 0.49 / 0.50 ---------------------------------
    # 注意: 恢复计时从睁眼证据首次出现时开始, 需先喂一帧睁眼启动计时,
    # 再在 rec 秒后判定 (与 A1 FT-07 的两步构造一致)。

    def test_e04_fatigue_recovery_boundary(self):
        for rec, expect_normal in ((0.49, False), (0.50, True)):
            sm = FatigueStateMachine(CFG)
            t = 200.0
            sm.update([det("open_eye")], t)
            sm.update([det("closed_eye")], t + 1.0)
            sm.update([det("closed_eye")], t + 2.5)      # FATIGUE
            r = sm.update([det("open_eye")], t + 2.5)    # 睁眼起点(恢复计时开始)
            self.assertEqual(r.state, FatigueState.FATIGUE, f"rec={rec} 起点")
            r = sm.update([det("open_eye")], t + 2.5 + rec)
            if expect_normal:
                self.assertEqual(r.state, FatigueState.NORMAL, f"rec={rec}")
            else:
                self.assertEqual(r.state, FatigueState.FATIGUE, f"rec={rec}")

    # -- E-05: 张嘴触发边界 0.99 / 1.00 --------------------------------

    def test_e05_open_mouth_boundary(self):
        for dur, expect_yawn in ((0.99, False), (1.00, True)):
            sm = FatigueStateMachine(CFG)
            t = 200.0
            sm.update([det("open_eye"), det("closed_mouth")], t)
            sm.update([det("open_eye"), det("open_mouth")], t + 1.0)  # 张嘴起点
            r = sm.update([det("open_eye"), det("open_mouth")], t + 1.0 + dur)
            if expect_yawn:
                self.assertEqual(r.state, FatigueState.YAWN, f"dur={dur}")
            else:
                self.assertNotEqual(r.state, FatigueState.YAWN, f"dur={dur}")

    # -- E-06: 眼部宽限边界 0.50 / 0.51 ---------------------------------
    # 依据共享基线第 5 节兜底规则: 上一输出 NORMAL 且仍在眼部宽限内
    # (eye_missing_grace=0.5s) 才保持 NORMAL, 宽限外即转 UNKNOWN;
    # unknown_timeout=1.0s 在非 FATIGUE 路径不参与判定(与 A1 FT-05 一致),
    # 该参数在 FATIGUE 锁存期间的边界由 E-13 验证。

    def test_e06_eye_missing_grace_boundary(self):
        for gap, expect_unknown in ((0.50, False), (0.51, True)):
            sm = FatigueStateMachine(CFG)
            t = 200.0
            sm.update([det("open_eye")], t)              # NORMAL, last=200
            r = sm.update([], t + gap)
            if expect_unknown:
                self.assertEqual(r.state, FatigueState.UNKNOWN, f"gap={gap}")
            else:
                self.assertNotEqual(r.state, FatigueState.UNKNOWN, f"gap={gap}")

    # -- E-07: 初始 UNKNOWN（启动后仅闭眼，不谎报 NORMAL） -------------

    def test_e07_initial_closed_eye_stays_unknown(self):
        sm = FatigueStateMachine(CFG)
        t = 200.0
        r = sm.update([det("closed_eye")], t)
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        r = sm.update([det("closed_eye")], t + 0.5)
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertNotEqual(r.state, FatigueState.NORMAL)
        # 无输入流保持 UNKNOWN
        r = sm.update([], t + 1.0)
        self.assertEqual(r.state, FatigueState.UNKNOWN)

    # -- E-08: 低置信度边界 0.49 / 0.50 ---------------------------------

    def test_e08_confidence_min_boundary(self):
        for conf, participates in ((0.49, False), (0.50, True)):
            sm = FatigueStateMachine(CFG)
            t = 200.0
            sm.update([det("open_eye")], t)              # NORMAL
            low = Detection(0, "closed_eye", conf, (0, 0, 1, 1))
            sm.update([low], t + 1.0)                    # 闭眼(低置信度)起点候选
            r = sm.update([low], t + 2.5)                # 1.5s 后
            if participates:
                self.assertEqual(r.state, FatigueState.FATIGUE, f"conf={conf}")
            else:
                self.assertNotEqual(r.state, FatigueState.FATIGUE, f"conf={conf}")

    # -- E-09: 未知类别名拒绝 -------------------------------------------

    def test_e09_unknown_class_rejected(self):
        sm = FatigueStateMachine(CFG)
        with self.assertRaises(ValueError) as cm:
            sm.update([{"class": "bogus", "confidence": 0.9}], 200.0)
        self.assertIn("bogus", str(cm.exception))
        # Detection 对象未知类别同样拒绝
        with self.assertRaises(ValueError):
            sm.update([Detection(9, "bogus", 0.9)], 200.0)

    # -- E-10: 多目标相反类别冲突 ---------------------------------------

    def test_e10_conflicting_multi_target(self):
        # 眼冲突: open_eye + closed_eye 同帧
        sm = FatigueStateMachine(CFG)
        t = 200.0
        sm.update([det("open_eye")], t)                  # NORMAL
        sm.update([det("open_eye"), det("closed_eye")], t + 0.5)
        r = sm.update([det("open_eye"), det("closed_eye")], t + 1.6)
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 不可靠超过 1.0s
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        # 嘴冲突: open_mouth + closed_mouth 同帧, 眼可靠
        sm2 = FatigueStateMachine(CFG)
        t = 200.0
        sm2.update([det("open_eye"), det("closed_mouth")], t)
        sm2.update([det("open_eye"), det("open_mouth"), det("closed_mouth")], t + 0.5)
        r = sm2.update([det("open_eye"), det("open_mouth"), det("closed_mouth")], t + 2.0)
        self.assertNotEqual(r.state, FatigueState.YAWN)  # 嘴证据不可靠, 不触发 YAWN
        self.assertEqual(r.state, FatigueState.NORMAL)   # 眼可靠 -> NORMAL

    # -- E-11: YAWN 保持期内闭嘴不退出；保持期结束边界退出 -------------

    def test_e11_yawn_min_hold_boundary(self):
        def _arm_yawn(sm):
            """返回 (sm, trigger_at): 触发 YAWN 后返回精确触发时刻。"""
            t = 200.0
            sm.update([det("open_eye"), det("closed_mouth")], t)
            t += dt
            sm.update([det("open_eye"), det("open_mouth")], t)   # 张嘴起点
            mouth_start = t
            while True:
                t += dt
                r = sm.update([det("open_eye"), det("open_mouth")], t)
                if r.state == FatigueState.YAWN:
                    break
            return mouth_start + 1.0

        dt = 0.01
        # 场景A: 保持期内(trigger_at+0.30 开始闭嘴, +0.30s 后累计 0.3s)
        # 保持期未结束(trigger_at+0.60 < trigger_at+1.00), 仍必须 YAWN。
        sm = FatigueStateMachine(CFG)
        trigger_at = _arm_yawn(sm)
        t_close = trigger_at + 0.30
        sm.update([det("open_eye"), det("closed_mouth")], t_close)
        r = sm.update([det("open_eye"), det("closed_mouth")], t_close + 0.30)
        self.assertEqual(r.state, FatigueState.YAWN, "保持期内闭嘴0.3s不得退出")
        # 场景B: 闭嘴持续到保持期结束边界 trigger_at+1.00 -> 恰好退出
        r = sm.update([det("open_eye"), det("closed_mouth")], trigger_at + 1.00)
        self.assertNotEqual(r.state, FatigueState.YAWN, "保持期结束+闭嘴0.3s 应退出")
        # 场景C: 保持期结束后才开始闭嘴, 0.29s 仍 YAWN, 0.30s 退出
        sm2 = FatigueStateMachine(CFG)
        trigger_at2 = _arm_yawn(sm2)
        t_close = trigger_at2 + 1.00 + dt   # 保持期已结束
        sm2.update([det("open_eye"), det("closed_mouth")], t_close)
        r = sm2.update([det("open_eye"), det("closed_mouth")], t_close + 0.29)
        self.assertEqual(r.state, FatigueState.YAWN, "闭嘴 0.29s 仍保持 YAWN")
        r = sm2.update([det("open_eye"), det("closed_mouth")], t_close + 0.30)
        self.assertNotEqual(r.state, FatigueState.YAWN, "闭嘴 0.30s 退出并重新武装")

    # -- E-12: 先 YAWN 后 FATIGUE，恢复后不回放、不重触发 ----------------

    def test_e12_yawn_then_fatigue_no_replay(self):
        dt = 0.01
        t = 200.0
        sm = FatigueStateMachine(CFG)
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)   # 张嘴起点
        mouth_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.YAWN:
                break
        trigger_at = mouth_start + 1.0
        # YAWN 锁存 0.2s 后闭眼
        t = trigger_at + 0.20
        sm.update([det("closed_eye"), det("open_mouth")], t)
        closed_start = t
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        self.assertEqual(r.state, FatigueState.FATIGUE)
        # FATIGUE 期间张嘴持续也不得出现 YAWN
        for _ in range(50):
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            self.assertEqual(r.state, FatigueState.FATIGUE)
        # 睁眼恢复 NORMAL
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        rec_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.NORMAL:
                break
            self.assertEqual(r.state, FatigueState.FATIGUE)
        rec_done = t
        # 恢复边界: 丢弃 FATIGUE 前/期间的嘴部证据并直接重新武装, 无需 closed_mouth
        snap = sm.snapshot_yawn(rec_done)
        self.assertTrue(snap.armed, "恢复边界应直接重新武装")
        self.assertIsNone(snap.mouth_open_seconds, "旧张嘴计时应被丢弃")
        # 恢复后新的连续张嘴须重新累计满 1.0s, 全程无 closed_mouth 也不回放旧 YAWN
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        fresh_start = t
        t += dt
        while t - fresh_start < 1.0 - 1e-9:
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertNotEqual(r.state, FatigueState.YAWN, f"t={t} 不应重触发")
            t += dt
        r = sm.update([det("open_eye"), det("open_mouth")], t)
        self.assertEqual(r.state, FatigueState.YAWN, "新满 1.0s 张嘴触发, 未用 closed_mouth")

    # -- E-13: FATIGUE 证据丢失 0.99 / 1.00 -----------------------------

    def test_e13_fatigue_evidence_loss(self):
        sm = FatigueStateMachine(CFG)
        t = 200.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)
        r = sm.update([det("closed_eye")], t + 2.5)      # FATIGUE
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([], t + 3.49)                      # 丢失 0.99s
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([], t + 3.50)                      # 丢失 1.00s
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertNotEqual(r.state, FatigueState.NORMAL, "不得直接恢复 NORMAL")
        # 清锁存后: 睁眼 0.5s 后 NORMAL
        r = sm.update([det("open_eye")], t + 4.0)
        self.assertEqual(r.state, FatigueState.NORMAL)

    # -- E-14: 配置校验（独立构造） -------------------------------------

    def test_e14_config_validation(self):
        base = dict(CFG)
        # 文件不存在
        with self.assertRaises(ValueError) as cm:
            load_fatigue_config(os.path.join(tempfile.gettempdir(), "no_such_cfg.yaml"))
        self.assertIn("不存在", str(cm.exception))
        # 顶层非映射
        fd, path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump([1, 2, 3], f)
            with self.assertRaises(ValueError) as cm:
                load_fatigue_config(path)
            self.assertIn("映射", str(cm.exception))
        finally:
            os.unlink(path)
        # 缺字段
        bad = dict(base)
        del bad["open_mouth_trigger"]
        self._expect_field_error(bad, "open_mouth_trigger")
        # 未知字段
        bad = dict(base)
        bad["unknown_extra"] = 1.0
        self._expect_field_error(bad, "unknown_extra")
        # 字符串类型
        bad = dict(base)
        bad["closed_eye_trigger"] = "1.5"
        self._expect_field_error(bad, "closed_eye_trigger")
        # 负值
        bad = dict(base)
        bad["unknown_timeout"] = -0.5
        self._expect_field_error(bad, "unknown_timeout")
        # 越界
        bad = dict(base)
        bad["evidence_confidence_min"] = 1.01
        self._expect_field_error(bad, "evidence_confidence_min")
        # bool
        bad = dict(base)
        bad["fatigue_recovery"] = True
        self._expect_field_error(bad, "fatigue_recovery")

    def _expect_field_error(self, data, field):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)
            with self.assertRaises(ValueError) as cm:
                load_fatigue_config(path)
            self.assertIn(field, str(cm.exception))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- E-15: evidence_score 语义 --------------------------------------

    def test_e15_evidence_score_semantics(self):
        # FATIGUE -> closed_eye 最高置信度
        sm = FatigueStateMachine(CFG)
        t = 200.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye", 0.83)], t + 1.0)
        r = sm.update([det("closed_eye", 0.83)], t + 2.5)
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertEqual(r.evidence_score, 83)
        # UNKNOWN -> 0
        r = sm.update([], t + 3.5)
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertEqual(r.evidence_score, 0)
        # NORMAL -> open_eye
        sm2 = FatigueStateMachine(CFG)
        r = sm2.update([det("open_eye", 0.77)], 300.0)
        self.assertEqual(r.state, FatigueState.NORMAL)
        self.assertEqual(r.evidence_score, 77)

    # -- E-16: 空检测流 ------------------------------------------------

    def test_e16_empty_stream_unknown(self):
        sm = FatigueStateMachine(CFG)
        t = 200.0
        for i in range(10):
            r = sm.update([], t + i * 0.5)
            self.assertEqual(r.state, FatigueState.UNKNOWN)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(A4ExtendedStateMachineTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
