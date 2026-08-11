"""G2-01 疲劳状态机验收测试:FT-01..FT-22(标准库 unittest)。

运行:
    python -m unittest tests.test_fatigue_state -v
或:
    python tests/test_fatigue_state.py
工作目录:仓库根。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "host_app",
    ),
)

import yaml  # noqa: E402

from fatigue_state import (  # noqa: E402
    CLASS_NAMES,
    DEFAULT_CONFIG_PATH,
    Detection,
    FatigueState,
    FatigueStateMachine,
    YawnSnapshot,
    load_fatigue_config,
)

CFG = load_fatigue_config(DEFAULT_CONFIG_PATH)


def det(name, conf=0.90):
    """构造一个高置信度 Detection(conf >= evidence_confidence_min=0.50)。"""
    return Detection(
        class_id=CLASS_NAMES.index(name),
        class_name=name,
        confidence=conf,
        bbox=(1, 2, 3, 4),
    )


class FatigueStateMachineTest(unittest.TestCase):
    # -- FT-01 ~ FT-05 闭眼计时 ------------------------------------------

    def test_ft01_blink_020s_no_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)          # 可靠睁眼 -> NORMAL
        r = sm.update([det("closed_eye")], t + 0.2)  # 闭眼 0.20 秒
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([det("open_eye")], t + 0.3)  # 睁眼
        self.assertEqual(r.state, FatigueState.NORMAL)

    def test_ft02_closed_149s_no_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)    # 闭眼起点
        r = sm.update([det("closed_eye")], t + 2.49)  # 闭眼 1.49 秒
        self.assertNotEqual(r.state, FatigueState.FATIGUE)

    def test_ft03_closed_150s_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)    # 闭眼起点
        r = sm.update([det("closed_eye")], t + 2.5)  # 闭眼 1.50 秒
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertEqual(r.evidence_score, 90)     # closed_eye 0.90 -> 90
        self.assertGreaterEqual(r.duration_ms, 0)

    def test_ft04_missing_020s_keeps_timer(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)    # 闭眼起点 101.0
        sm.update([det("closed_eye")], t + 2.0)    # 闭眼 1.0 秒
        sm.update([], t + 2.2)                     # 漏检 0.2 秒(<0.5 宽限,起点保留)
        r = sm.update([det("closed_eye")], t + 2.3)  # 总 1.3 秒,不触发
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([det("closed_eye")], t + 2.5)  # 起点保留 -> 总 1.5 秒触发
        self.assertEqual(r.state, FatigueState.FATIGUE)

    def test_ft05_missing_over_050s_resets(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)    # 闭眼起点 101.0
        r = sm.update([], t + 1.51)                # 漏检 0.51 秒 > 0.5 宽限
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 宽限外转 UNKNOWN
        r = sm.update([det("closed_eye")], t + 2.0)  # 新闭眼起点(计时已清零)
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 启动后无可靠睁眼史?不,有;闭眼 0.5s 未达阈值且上一输出 UNKNOWN
        r = sm.update([det("closed_eye")], t + 3.5)  # 新计时 1.5 秒才触发
        self.assertEqual(r.state, FatigueState.FATIGUE)

    # -- FT-06 ~ FT-07 FATIGUE 恢复 ---------------------------------------

    def test_ft06_recovery_049s_stays_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)
        sm.update([det("closed_eye")], t + 2.5)    # FATIGUE
        r = sm.update([det("open_eye")], t + 2.99)  # 睁眼 0.49 秒
        self.assertEqual(r.state, FatigueState.FATIGUE)

    def test_ft07_recovery_050s_clears_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)
        sm.update([det("closed_eye")], t + 2.5)    # FATIGUE
        r = sm.update([det("open_eye")], t + 3.0)  # 睁眼恢复起点
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([det("open_eye")], t + 3.5)  # 睁眼 0.50 秒 -> NORMAL
        self.assertEqual(r.state, FatigueState.NORMAL)
        self.assertEqual(r.evidence_score, 90)     # open_eye 0.90 -> 90

    # -- FT-08 ~ FT-09 YAWN -----------------------------------------------

    def test_ft08_open_mouth_020s_no_yawn(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)  # NORMAL
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)  # 张嘴起点
        r = sm.update([det("open_eye"), det("open_mouth")], t + 1.2)
        self.assertNotEqual(r.state, FatigueState.YAWN)

    def test_ft09_open_mouth_100s_yawn(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)  # 张嘴起点
        r = sm.update([det("open_eye"), det("open_mouth")], t + 2.0)  # 张嘴 1.00 秒
        self.assertEqual(r.state, FatigueState.YAWN)
        self.assertEqual(r.evidence_score, 90)     # open_mouth 0.90 -> 90

    # -- FT-10 冲突优先级 ------------------------------------------------

    def test_ft10_fatigue_and_yawn_same_time(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("closed_eye"), det("closed_mouth")], t + 1.0)  # 闭眼起点
        closed_start = t + 1.0
        sm.update([det("closed_eye"), det("open_mouth")], t + 1.5)    # 张嘴起点
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        # 闭眼 1.5s 与张嘴 1.0s 同刻成立 -> 只输出 FATIGUE
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6)
        # FATIGUE 期间禁止 YAWN
        for _ in range(20):
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            self.assertEqual(r.state, FatigueState.FATIGUE)

    # -- FT-11 UNKNOWN ----------------------------------------------------

    def test_ft11_no_target_1s_unknown(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)          # NORMAL
        r = sm.update([], t + 1.0)               # 无可靠目标持续 1 秒
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertEqual(r.evidence_score, 0)

    # -- FT-12 帧率无关性 -------------------------------------------------

    def test_ft12_fps_independent_timing(self):
        for fps in (10, 20, 30):
            dt = 1.0 / fps
            sm = FatigueStateMachine(CFG)
            t = 100.0
            sm.update([det("open_eye")], t)
            t += dt
            sm.update([det("closed_eye")], t)    # 闭眼起点
            closed_start = t
            while True:
                t += dt
                r = sm.update([det("closed_eye")], t)
                if r.state == FatigueState.FATIGUE:
                    break
            err = abs(t - (closed_start + 1.5))
            self.assertLessEqual(
                err, dt + 1e-6, f"fps={fps}: 触发帧 t={t}, 误差 {err} 超过一个采样周期 {dt}"
            )

    # -- FT-13 20 次短眨眼 ------------------------------------------------

    def test_ft13_20_blinks_no_fatigue(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        for _ in range(20):
            t += 0.15
            r = sm.update([det("closed_eye")], t)
            t += 0.15
            r = sm.update([det("closed_eye")], t)
            self.assertNotEqual(r.state, FatigueState.FATIGUE)
            t += 0.15
            r = sm.update([det("open_eye")], t)
            t += 0.15
            r = sm.update([det("open_eye")], t)
            self.assertNotEqual(r.state, FatigueState.FATIGUE)
        self.assertEqual(r.state, FatigueState.NORMAL)

    # -- FT-14 启动后仅闭眼 -----------------------------------------------

    def test_ft14_only_closed_eye_after_start(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        r = sm.update([det("closed_eye")], t)
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 初始 UNKNOWN
        r = sm.update([det("closed_eye")], t + 0.5)
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 不谎报 NORMAL
        self.assertNotEqual(r.state, FatigueState.NORMAL)

    # -- FT-15 冲突眼证据 -------------------------------------------------

    def test_ft15_mixed_eyes_unreliable(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)            # NORMAL
        sm.update([det("closed_eye")], t + 1.0)    # 闭眼起点(眨眼)
        sm.update([det("closed_eye")], t + 2.0)    # 闭眼 1.0 秒,仍 NORMAL
        mixed = [det("open_eye"), det("closed_eye")]
        sm.update(mixed, t + 2.1)                  # 混合 0.1s:宽限内保留计时
        r = sm.update(mixed, t + 2.51)             # 混合 0.51s > 0.5:眼部不可靠,计时清零
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        sm.update([det("closed_eye")], t + 3.0)    # 新闭眼起点
        r = sm.update([det("closed_eye")], t + 3.5)  # 新计时 0.5s:不累计旧计时
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([det("closed_eye")], t + 4.5)  # 新计时 1.5s 才触发
        self.assertEqual(r.state, FatigueState.FATIGUE)

    # -- FT-16 多目标相反类别 ---------------------------------------------

    def test_ft16_multi_target_conflict(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)  # NORMAL
        mixed = [
            det("open_eye"),
            det("closed_eye"),
            det("open_mouth"),
            det("closed_mouth"),
        ]
        sm.update(mixed, t + 0.1)
        sm.update(mixed, t + 0.5)                  # 两类模态都不可靠
        r = sm.update(mixed, t + 1.2)              # 持续无可靠 -> UNKNOWN
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertNotEqual(r.state, FatigueState.FATIGUE)
        self.assertNotEqual(r.state, FatigueState.YAWN)

    # -- FT-17 YAWN 最短保持与退出 ----------------------------------------

    def test_ft17_yawn_min_hold_then_exit(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)  # 张嘴起点
        mouth_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.YAWN:
                break
        trigger_at = mouth_start + 1.0             # 精确触发时刻
        self.assertLessEqual(abs(t - trigger_at), dt + 1e-6)
        # 下一帧起持续可靠闭嘴:触发后 0.99 秒内仍为 YAWN
        t += dt
        while t - trigger_at < 0.99:
            r = sm.update([det("open_eye"), det("closed_mouth")], t)
            self.assertEqual(r.state, FatigueState.YAWN, f"t={t} 应保持 YAWN")
            t += dt
        # 保持期结束 = trigger_at + 1.0;闭嘴已远超 0.3s,保持期到即退出
        t_end = trigger_at + 1.0
        while t < t_end - 1e-12:
            r = sm.update([det("open_eye"), det("closed_mouth")], t)
            self.assertEqual(r.state, FatigueState.YAWN)
            t += dt
        r = sm.update([det("open_eye"), det("closed_mouth")], t)
        self.assertEqual(r.state, FatigueState.NORMAL)  # 退出并重新武装
        # 重新武装验证:新张嘴 1.0s 再次触发
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        new_start = t
        t += dt
        while t - new_start < 1.0 - 1e-9:
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertNotEqual(r.state, FatigueState.YAWN)
            t += dt
        r = sm.update([det("open_eye"), det("open_mouth")], t)
        self.assertEqual(r.state, FatigueState.YAWN)

    # -- FT-18 保持期后闭嘴 0.29/0.30 秒 ----------------------------------

    def test_ft18_yawn_exit_closed_mouth_030(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)  # 张嘴起点
        mouth_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.YAWN:
                break
        trigger_at = mouth_start + 1.0
        t_hold_end = trigger_at + 1.0
        # 保持期内持续张嘴(不闭嘴)
        while t < t_hold_end - 1e-12:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertEqual(r.state, FatigueState.YAWN)
        # 保持期结束后开始闭嘴:0.29 秒仍 YAWN,0.30 秒退出并重新武装
        t += dt
        sm.update([det("open_eye"), det("closed_mouth")], t)
        closed_start = t
        r = sm.update([det("open_eye"), det("closed_mouth")], closed_start + 0.29)
        self.assertEqual(r.state, FatigueState.YAWN)
        r = sm.update([det("open_eye"), det("closed_mouth")], closed_start + 0.30)
        self.assertNotEqual(r.state, FatigueState.YAWN)
        self.assertEqual(r.state, FatigueState.NORMAL)

    # -- FT-23 ~ FT-26 主机侧 YAWN 恢复兜底 ---------------------------------

    def test_ft23_yawn_open_eye_without_open_mouth_exits_at_030(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)
        r = sm.update([det("open_eye"), det("open_mouth")], t + 2.0)
        self.assertEqual(r.state, FatigueState.YAWN)

        # 最短保持期结束后，只有睁眼、没有有效 open_mouth。
        fallback_start = t + 3.1
        sm.update([det("open_eye")], fallback_start)
        r = sm.update([det("open_eye")], fallback_start + 0.29)
        self.assertEqual(r.state, FatigueState.YAWN)
        r = sm.update([det("open_eye")], fallback_start + 0.30)
        self.assertEqual(r.state, FatigueState.NORMAL)

    def test_ft24_fresh_open_mouth_resets_yawn_fallback(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)
        r = sm.update([det("open_eye"), det("open_mouth")], t + 2.0)
        self.assertEqual(r.state, FatigueState.YAWN)

        fallback_start = t + 3.1
        sm.update([det("open_eye")], fallback_start)
        sm.update([det("open_eye")], fallback_start + 0.25)
        sm.update([det("open_eye"), det("open_mouth")], fallback_start + 0.25)
        restart = fallback_start + 0.26
        sm.update([det("open_eye")], restart)
        r = sm.update([det("open_eye")], restart + 0.29)
        self.assertEqual(r.state, FatigueState.YAWN)
        r = sm.update([det("open_eye")], restart + 0.30)
        self.assertEqual(r.state, FatigueState.NORMAL)

    def test_ft25_yawn_fallback_requires_reliable_open_eye(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)
        r = sm.update([det("open_eye"), det("open_mouth")], t + 2.0)
        self.assertEqual(r.state, FatigueState.YAWN)

        # 无眼部证据尚未到 UNKNOWN 超时，不能借兜底离开 YAWN。
        sm.update([], t + 2.0)
        r = sm.update([], t + 2.89)
        self.assertEqual(r.state, FatigueState.YAWN)

    def test_ft26_closed_mouth_recovery_remains_available(self):
        cfg = dict(CFG)
        cfg["yawn_fallback_open_eye"] = 99.0
        sm = FatigueStateMachine(cfg)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)
        r = sm.update([det("open_eye"), det("open_mouth")], t + 2.0)
        self.assertEqual(r.state, FatigueState.YAWN)

        sm.update([det("open_eye"), det("closed_mouth")], t + 2.01)
        r = sm.update([det("open_eye"), det("closed_mouth")], t + 3.0)
        self.assertEqual(r.state, FatigueState.NORMAL)

    # -- FT-19 FATIGUE 与 YAWN 同刻 ---------------------------------------

    def test_ft19_fatigue_overrides_yawn_same_time(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("closed_eye"), det("open_mouth")], t)  # 闭眼+张嘴同起点
        both_start = t
        closed_start = t
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6)
        # 之后张嘴持续,FATIGUE 期间禁止 YAWN
        for _ in range(20):
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            self.assertEqual(r.state, FatigueState.FATIGUE)

    # -- FT-20 FATIGUE 后证据缺失 0.99/1.00 秒 ----------------------------

    def test_ft20_fatigue_missing_099_100(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)
        r = sm.update([det("closed_eye")], t + 2.5)  # FATIGUE,last=102.5
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([], t + 3.49)                  # 缺失 0.99 秒:保持 FATIGUE
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update([], t + 3.5)                   # 缺失 1.00 秒:UNKNOWN 且不转 NORMAL
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertNotEqual(r.state, FatigueState.NORMAL)

    def test_ft20b_fatigue_mixed_conflict_evidence(self):
        """FATIGUE 期间混合冲突眼证据(open+closed)连续 1.0s -> UNKNOWN 并清锁存。"""
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        sm.update([det("closed_eye")], t + 1.0)
        r = sm.update([det("closed_eye")], t + 2.5)  # FATIGUE
        self.assertEqual(r.state, FatigueState.FATIGUE)
        mixed = [det("open_eye"), det("closed_eye")]
        sm.update(mixed, t + 2.6)                    # 混合证据起点(不可靠)
        r = sm.update(mixed, t + 3.49)               # 不可靠 0.89s:仍 FATIGUE
        self.assertEqual(r.state, FatigueState.FATIGUE)
        r = sm.update(mixed, t + 3.5)                # 不可靠 1.00s:UNKNOWN,清疲劳锁存
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        self.assertNotEqual(r.state, FatigueState.NORMAL)

    # -- FT-21 配置校验 ----------------------------------------------------

    def test_ft21_config_validation(self):
        base = dict(CFG)
        self.assertEqual(base["yawn_fallback_open_eye"], 0.3)
        # 缺字段
        bad = dict(base)
        del bad["closed_eye_trigger"]
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("closed_eye_trigger", str(cm.exception))
        # 未知字段
        bad = dict(base)
        bad["bogus_field"] = 1.0
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("bogus_field", str(cm.exception))
        # 错误类型
        bad = dict(base)
        bad["closed_eye_trigger"] = "1.5"
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("closed_eye_trigger", str(cm.exception))
        # 负时长
        bad = dict(base)
        bad["closed_eye_trigger"] = -1.0
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("closed_eye_trigger", str(cm.exception))
        # 置信度越界
        bad = dict(base)
        bad["evidence_confidence_min"] = 1.5
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("evidence_confidence_min", str(cm.exception))
        # bool 不得当作数字
        bad = dict(base)
        bad["eye_missing_grace"] = True
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("eye_missing_grace", str(cm.exception))

    @staticmethod
    def _load(data):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)
            return load_fatigue_config(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- FT-22 先 YAWN 后 FATIGUE,恢复后不回放 ----------------------------

    def test_ft22_yawn_then_fatigue_no_replay(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)  # 张嘴起点
        mouth_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.YAWN:
                break
        trigger_at = mouth_start + 1.0
        # YAWN 已锁存;锁存 0.2 秒后开始闭眼(嘴继续张嘴)
        t = trigger_at + 0.20
        sm.update([det("closed_eye"), det("open_mouth")], t)  # 闭眼起点
        closed_start = t
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6)
        # 恢复:持续睁眼(嘴继续张嘴),0.5 秒内保持 FATIGUE,0.5 秒后恢复 NORMAL
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        rec_start = t
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.NORMAL:
                break
            self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertAlmostEqual(t - rec_start, 0.5, places=4)
        self.assertEqual(r.state, FatigueState.NORMAL)
        # 恢复边界语义:丢弃 FATIGUE 前/期间的嘴部与 YAWN 证据并直接重新武装,
        # 无需 closed_mouth;旧张嘴计时清零,不可回放。
        rec_done = t  # 恢复完成帧(恢复边界)
        snap = sm.snapshot_yawn(rec_done)
        self.assertTrue(snap.armed)                  # 恢复即重新武装
        self.assertIsNone(snap.mouth_open_seconds)   # 旧张嘴计时已丢弃
        # 恢复边界后张嘴持续:全程无任何 closed_mouth,旧时长不回放,须重新累计满 1.0s
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        fresh_start = t  # 恢复边界后首帧:新张嘴计时起点
        t += dt
        while t - fresh_start < 1.0 - 1e-9:
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertNotEqual(r.state, FatigueState.YAWN, f"t={t} 不应重触发")
            t += dt
        r = sm.update([det("open_eye"), det("open_mouth")], t)
        self.assertEqual(r.state, FatigueState.YAWN)  # 无需 closed_mouth,新满 1.0s 才触发

    def test_ft27_fatigue_recovery_rearms_without_closed_mouth(self):
        """FT-27 诊断场景:闭眼进入 FATIGUE(期间持续张嘴,模型不发 closed_mouth),
        睁眼恢复后丢弃旧张嘴计时并直接重新武装;无需任何 closed_mouth 也能以新的
        完整 1.0s 张嘴重新触发 YAWN,且恢复瞬间绝不回放旧 YAWN。"""
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("open_mouth")], t)   # NORMAL,张嘴计时启动
        t += dt
        sm.update([det("closed_eye"), det("open_mouth")], t)  # 闭眼起点(嘴持续张开)
        closed_start = t
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        self.assertEqual(r.state, FatigueState.FATIGUE)
        self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6)
        # FATIGUE 期间张嘴持续,禁止 YAWN
        for _ in range(20):
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            self.assertEqual(r.state, FatigueState.FATIGUE)
        # 睁眼恢复:0.5s 内保持 FATIGUE,随后 NORMAL
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
        # 恢复边界:重新武装且丢弃旧张嘴计时(全程无 closed_mouth)
        snap = sm.snapshot_yawn(rec_done)
        self.assertTrue(snap.armed)
        self.assertIsNone(snap.mouth_open_seconds)
        # 恢复边界后首帧起重新累计;旧张嘴时长不回放,必须满 1.0s 才触发
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        fresh_start = t
        t += dt
        while t - fresh_start < 1.0 - 1e-9:
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertNotEqual(r.state, FatigueState.YAWN, f"t={t} 不应重触发")
            t += dt
        r = sm.update([det("open_eye"), det("open_mouth")], t)
        self.assertEqual(r.state, FatigueState.YAWN)

    # -- 补充:置信度过滤与输出字段 ---------------------------------------

    def test_ft28_fatigue_timeout_rearms_without_closed_mouth(self):
        """FATIGUE 因眼部超时退出后，也必须丢弃旧嘴部计时并重新武装。"""
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("closed_eye"), det("open_mouth")], t)
        closed_start = t
        while True:
            t += dt
            r = sm.update([det("closed_eye"), det("open_mouth")], t)
            if r.state == FatigueState.FATIGUE:
                break
        self.assertLessEqual(abs(t - (closed_start + 1.5)), dt + 1e-6)

        # 眼部证据缺失超时退出 FATIGUE；不能遗留“未武装 + 旧张嘴计时”。
        t += CFG["unknown_timeout"] + dt
        r = sm.update([], t)
        self.assertEqual(r.state, FatigueState.UNKNOWN)
        snap = sm.snapshot_yawn(t)
        self.assertTrue(snap.armed)
        self.assertIsNone(snap.mouth_open_seconds)

        # 无 closed_mouth 时，恢复 NORMAL 后的张嘴仍须从零累计完整 1.0 秒。
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        fresh_start = t
        t += dt
        while t - fresh_start < CFG["open_mouth_trigger"] - 1e-9:
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            self.assertNotEqual(r.state, FatigueState.YAWN)
            t += dt
        r = sm.update([det("open_eye"), det("open_mouth")], t)
        self.assertEqual(r.state, FatigueState.YAWN)

    def test_confidence_below_min_ignored(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye")], t)
        # 低置信度闭眼(0.30 < 0.50)不参与证据聚合:不累计闭眼计时
        low = Detection(0, "closed_eye", 0.30, (0, 0, 1, 1))
        sm.update([low], t + 0.5)
        r = sm.update([low], t + 1.0)
        self.assertEqual(r.state, FatigueState.UNKNOWN)  # 无可靠眼部证据,按超时规则
        self.assertNotEqual(r.state, FatigueState.FATIGUE)  # 低置信度不累计闭眼

    def test_result_fields_and_dict_input(self):
        sm = FatigueStateMachine(CFG)
        r = sm.update([{"class": "open_eye", "confidence": 0.9}], 100.0)
        self.assertEqual(r.state, FatigueState.NORMAL)
        self.assertEqual(r.timestamp_ms, 100_000)
        self.assertEqual(r.evidence_score, 90)
        self.assertGreaterEqual(r.duration_ms, 0)

    def test_initial_state_unknown(self):
        sm = FatigueStateMachine(CFG)
        r = sm.update([], 100.0)
        self.assertEqual(r.state, FatigueState.UNKNOWN)

    # -- YAWN 诊断快照 -------------------------------------------------

    def test_yawn_snapshot_before_open_mouth(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        snap = sm.snapshot_yawn(t + 0.1)
        self.assertTrue(snap.armed)
        self.assertFalse(snap.active)
        self.assertIsNone(snap.mouth_open_seconds)

    def test_yawn_snapshot_while_timing(self):
        sm = FatigueStateMachine(CFG)
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        sm.update([det("open_eye"), det("open_mouth")], t + 1.0)
        snap = sm.snapshot_yawn(t + 1.5)
        self.assertTrue(snap.armed)
        self.assertFalse(snap.active)
        self.assertAlmostEqual(snap.mouth_open_seconds, 0.5, places=4)

    def test_yawn_snapshot_after_trigger(self):
        sm = FatigueStateMachine(CFG)
        dt = 0.01
        t = 100.0
        sm.update([det("open_eye"), det("closed_mouth")], t)
        t += dt
        sm.update([det("open_eye"), det("open_mouth")], t)
        while True:
            t += dt
            r = sm.update([det("open_eye"), det("open_mouth")], t)
            if r.state == FatigueState.YAWN:
                break
        snap = sm.snapshot_yawn(t)
        self.assertFalse(snap.armed)
        self.assertTrue(snap.active)
        self.assertAlmostEqual(snap.mouth_open_seconds, 1.0, places=2)

    def test_unknown_class_name_rejected(self):
        sm = FatigueStateMachine(CFG)
        with self.assertRaises(ValueError) as cm:
            sm.update([{"class": "bogus", "confidence": 0.9}], 100.0)
        self.assertIn("bogus", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
