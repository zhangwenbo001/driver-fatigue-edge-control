"""
疲劳时序状态机 —— 与 GUI/串口/硬件完全解耦的确定性算法模块。

职责:
  - Detection       统一检测数据结构(class_id / class_name / confidence / bbox)
  - FatigueResult   状态机输出(state / evidence_score / duration_ms / timestamp_ms)
  - FatigueState    主机状态枚举(NORMAL / YAWN / FATIGUE / UNKNOWN)
  - load_fatigue_config()  配置加载与字段白名单/类型/范围/缺失项校验
  - FatigueStateMachine    全帧证据聚合 + 疲劳时序逻辑

约束（公开发布基线）:
  - 不导入 PySide6、串口库、Ultralytics 或任何硬件库。
  - now 必须允许测试注入;生产默认使用 time.monotonic()。
  - 不按帧数累计时长,所有计时基于单调时钟。
  - 决策唯一顺序:FATIGUE 触发/锁存 > 眼部证据超时 UNKNOWN > YAWN 锁存
    > 可靠睁眼 NORMAL;兜底规则见 update()。
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyYAML 是 ultralytics 的传递依赖,但此处显式依赖以独立使用
    import yaml
except ImportError:  # pragma: no cover - 安装依赖后不会走到
    yaml = None

# 默认配置路径:仓库根目录 configs/fatigue.yaml(本文件位于 <根>/基于YOLOv8的疲劳驾驶检测系统/)
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "fatigue.yaml"
)

# 共享类别(与模型 names 严格一致,唯一真源在共享基线第 2 节)
CLASS_NAMES = ("closed_eye", "closed_mouth", "open_eye", "open_mouth")


class FatigueState(str, enum.Enum):
    """主机可发送的四个状态枚举,取值与 DMS1 协议一致。"""

    NORMAL = "NORMAL"
    YAWN = "YAWN"
    FATIGUE = "FATIGUE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Detection:
    """单目标结构化检测结果。detect() 只返回此类对象,不直接决定硬件输出。

    为兼容旧代码的 dict 下标访问,提供 __getitem__ 映射:
    d['class'] -> class_name,d['confidence'] -> confidence,d['bbox'] -> bbox。
    """

    class_id: int
    class_name: str
    confidence: float
    bbox: tuple = field(default=(0, 0, 0, 0))  # (x1, y1, x2, y2) 像素坐标

    def __getitem__(self, key):
        """兼容旧 dict 接口:['class'|'class_name', 'confidence', 'bbox']。"""
        if key in ("class", "class_name"):
            return self.class_name
        if key == "confidence":
            return self.confidence
        if key == "bbox":
            return self.bbox
        raise KeyError(key)


@dataclass(frozen=True)
class FatigueResult:
    """状态机单次更新输出。

    state:         FatigueState 枚举
    evidence_score: 当前输出状态对应类别的最高有效置信度换算为 0-100 整数;
                    UNKNOWN 固定为 0;若当前帧无该状态对应类别的检测(如锁存
                    保持但当帧缺证据),计 0。它是检测证据,不是疲劳概率。
    duration_ms:    当前状态已持续毫秒(单调时钟)
    timestamp_ms:   本次更新的单调时钟毫秒
    """

    state: FatigueState
    evidence_score: int
    duration_ms: int
    timestamp_ms: int


@dataclass(frozen=True)
class YawnSnapshot:
    """只读 YAWN 诊断快照 —— 武装状态 / 活跃状态 / 连续张嘴计时(无副作用)。"""

    armed: bool
    active: bool
    mouth_open_seconds: float | None


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

# 字段白名单:名称 -> (类型, 最小值, 最大值);None 表示无上界
FATIGUE_CONFIG_FIELDS = {
    "closed_eye_trigger": (float, 0.0, None),
    "eye_missing_grace": (float, 0.0, None),
    "fatigue_recovery": (float, 0.0, None),
    "open_mouth_trigger": (float, 0.0, None),
    "mouth_missing_grace": (float, 0.0, None),
    "yawn_min_hold": (float, 0.0, None),
    "yawn_rearm_closed_mouth": (float, 0.0, None),
    "yawn_fallback_open_eye": (float, 0.0, None),
    "unknown_timeout": (float, 0.0, None),
    "result_stale_timeout": (float, 0.0, None),
    "evidence_confidence_min": (float, 0.0, 1.0),
}


def _is_number(value) -> bool:
    """int/float 为真;bool 是 int 子类,必须排除。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_fatigue_config(path=DEFAULT_CONFIG_PATH) -> dict:
    """加载并校验 fatigue.yaml。

    检查:文件可读、顶层为映射、字段白名单、缺失项、类型、范围。
    任何失败都抛出 ValueError 并明确指出具体字段,启动即失败,不静默回退。
    """
    if yaml is None:  # pragma: no cover
        raise RuntimeError("缺少 PyYAML 依赖,无法加载 configs/fatigue.yaml")

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件 YAML 解析失败: {path} ({exc})") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"配置文件顶层必须是键值映射: {path}")

    unknown = sorted(set(raw) - set(FATIGUE_CONFIG_FIELDS))
    if unknown:
        raise ValueError(
            f"配置包含未知字段: {', '.join(unknown)}"
            f"(允许: {', '.join(sorted(FATIGUE_CONFIG_FIELDS))})"
        )

    missing = sorted(set(FATIGUE_CONFIG_FIELDS) - set(raw))
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(missing)}")

    validated: dict = {}
    for name, (expected_type, lo, hi) in FATIGUE_CONFIG_FIELDS.items():
        value = raw[name]
        if not _is_number(value):
            raise ValueError(
                f"配置字段 {name} 类型错误: 期望数字,实际 {type(value).__name__}({value!r})"
            )
        number = float(value)
        if number < lo or (hi is not None and number > hi):
            limit = f"<= {hi}" if hi is not None else f">= {lo}"
            raise ValueError(f"配置字段 {name} 超出范围: {number} (要求 {limit})")
        validated[name] = number
    return validated


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

# 内部证据聚合结果(仅用于状态机内部)
_EYE_OPEN = "open"
_EYE_CLOSED = "closed"
_MOUTH_OPEN = "open"
_MOUTH_CLOSED = "closed"

# 浮点容差(秒):消除 time 值减法在二进制浮点下的边界误差,远小于时间精度
_EPS = 1e-9


class FatigueStateMachine:
    """基于单调时钟的疲劳时序状态机。

    用法:
        sm = FatigueStateMachine()
        result = sm.update(detections, now)   # now 可注入;生产省略 now

    detections: list[Detection] 或 list[dict](兼容 {'class'|'class_name', 'confidence'})。
    只有 confidence >= evidence_confidence_min 的检测参与证据聚合。
    """

    def __init__(self, config=None, clock=time.monotonic):
        """
        config: dict — load_fatigue_config() 的返回值;None 时加载默认配置。
        clock:  callable — 无参单调时钟,生产默认 time.monotonic。
        """
        self._cfg = load_fatigue_config() if config is None else dict(config)
        self._clock = clock

        now = self._clock()
        # 输出状态
        self._state = FatigueState.UNKNOWN
        self._state_since = now

        # 眼部
        self._eye_evidence = None          # _EYE_OPEN / _EYE_CLOSED / None(不可靠)
        self._eye_closed_started_at = None  # 连续可靠闭眼(容忍宽限)起点
        self._last_reliable_eye_at = None   # 最近一次可靠眼部证据时刻
        self._fatigue_latched = False
        self._fatigue_recovery_since = None  # 疲劳后连续可靠睁眼起点

        # 嘴部
        self._mouth_evidence = None        # _MOUTH_OPEN / _MOUTH_CLOSED / None
        self._mouth_open_started_at = None  # 连续可靠张嘴(容忍宽限)起点
        self._last_reliable_mouth_at = None
        self._yawn_armed = True            # 初始武装;被 FATIGUE 覆盖后置 False
        self._yawn_active = False
        self._yawn_triggered_at = None     # 精确触发时刻(张嘴起点 + open_mouth_trigger)
        self._mouth_closed_since = None    # YAWN 锁存后的连续可靠闭嘴起点
        self._rearm_closed_since = None    # 未武装时的连续可靠闭嘴起点(重新武装)
        self._yawn_fallback_eye_since = None  # YAWN 兜底: 连续可靠睁眼+无可靠张嘴起点

    # -- 对外接口 ----------------------------------------------------------

    def update(self, detections, now=None) -> FatigueResult:
        """聚合证据并更新状态,返回 FatigueResult。"""
        now = self._clock() if now is None else float(now)
        self._aggregate_evidence(detections)
        self._update_timers(now)

        # 唯一决策顺序:FATIGUE > 眼部超时 UNKNOWN > YAWN > 可靠睁眼 NORMAL
        if self._fatigue_latched:
            new_state = self._decide_fatigue_latched(now)
        elif (
            self._eye_closed_started_at is not None
            and now - self._eye_closed_started_at
            >= self._cfg["closed_eye_trigger"] - _EPS
        ):
            self._fatigue_latched = True
            self._clear_yawn()
            new_state = FatigueState.FATIGUE
        elif self._eye_timeout(now):
            new_state = FatigueState.UNKNOWN
        elif self._yawn_active:
            new_state = FatigueState.YAWN
        elif self._eye_evidence == _EYE_OPEN:
            new_state = FatigueState.NORMAL
        else:
            new_state = self._fallback(now)

        if new_state != self._state:
            self._state = new_state
            self._state_since = now

        return FatigueResult(
            state=self._state,
            evidence_score=self._evidence_score(new_state),
            duration_ms=int(round((now - self._state_since) * 1000)),
            timestamp_ms=int(round(now * 1000)),
        )

    def snapshot_yawn(self, now=None) -> YawnSnapshot:
        """返回 YAWN 武装/活跃/张嘴计时的只读快照,无任何突变或计时副作用。"""
        now = self._clock() if now is None else float(now)
        elapsed = None
        if self._mouth_open_started_at is not None:
            elapsed = now - self._mouth_open_started_at
        return YawnSnapshot(
            armed=self._yawn_armed,
            active=self._yawn_active,
            mouth_open_seconds=elapsed,
        )

    # -- 证据聚合 -----------------------------------------------------------

    def _aggregate_evidence(self, detections) -> None:
        """第 4.1 节:置信度过滤 + 全帧类别互斥聚合。"""
        conf_min = self._cfg["evidence_confidence_min"]
        has_open_eye = has_closed_eye = False
        has_open_mouth = has_closed_mouth = False
        best_conf = {name: 0.0 for name in CLASS_NAMES}
        for d in detections:
            name, conf = self._item(d)
            if conf < conf_min:
                continue
            best_conf[name] = max(best_conf[name], conf)
            if name == "open_eye":
                has_open_eye = True
            elif name == "closed_eye":
                has_closed_eye = True
            elif name == "open_mouth":
                has_open_mouth = True
            elif name == "closed_mouth":
                has_closed_mouth = True
        self._best_conf = best_conf

        if has_open_eye and not has_closed_eye:
            self._eye_evidence = _EYE_OPEN
        elif has_closed_eye and not has_open_eye:
            self._eye_evidence = _EYE_CLOSED
        else:  # 两类同时存在或都不存在 -> 不可靠
            self._eye_evidence = None

        if has_open_mouth and not has_closed_mouth:
            self._mouth_evidence = _MOUTH_OPEN
        elif has_closed_mouth and not has_open_mouth:
            self._mouth_evidence = _MOUTH_CLOSED
        else:
            self._mouth_evidence = None

    @staticmethod
    def _item(d):
        """兼容 Detection 对象与 dict({class|class_name, confidence})。

        非法/未知类别名抛出明确 ValueError,不做静默忽略。
        """
        if isinstance(d, dict):
            name = d.get("class_name") or d.get("class")
            conf = float(d.get("confidence", 1.0))
        else:
            name = d.class_name
            conf = float(d.confidence)
        if name not in CLASS_NAMES:
            raise ValueError(f"检测结果含未知类别名 {name!r},允许: {list(CLASS_NAMES)}")
        return name, conf

    # -- 计时器 -------------------------------------------------------------

    def _update_timers(self, now) -> None:
        cfg = self._cfg

        # 眼部计时:可靠闭眼开始/可靠睁眼清零/不可靠超过宽限清零
        if self._eye_evidence == _EYE_OPEN:
            self._eye_closed_started_at = None
            self._last_reliable_eye_at = now
        elif self._eye_evidence == _EYE_CLOSED:
            if self._eye_closed_started_at is None:
                self._eye_closed_started_at = now
            self._last_reliable_eye_at = now
        else:
            if (
                self._last_reliable_eye_at is not None
                and now - self._last_reliable_eye_at > cfg["eye_missing_grace"] + _EPS
            ):
                self._eye_closed_started_at = None

        # 疲劳恢复计时:只有连续可靠睁眼累计,任何其他眼部证据清零
        if self._fatigue_latched:
            if self._eye_evidence == _EYE_OPEN:
                if self._fatigue_recovery_since is None:
                    self._fatigue_recovery_since = now
            else:
                self._fatigue_recovery_since = None

        # 嘴部计时:可靠张嘴开始/可靠闭嘴清零/不可靠超过宽限清零
        if self._mouth_evidence == _MOUTH_OPEN:
            if self._mouth_open_started_at is None:
                self._mouth_open_started_at = now
            self._last_reliable_mouth_at = now
        elif self._mouth_evidence == _MOUTH_CLOSED:
            self._mouth_open_started_at = None
            self._last_reliable_mouth_at = now
        else:
            if (
                self._last_reliable_mouth_at is not None
                and now - self._last_reliable_mouth_at > cfg["mouth_missing_grace"] + _EPS
            ):
                self._mouth_open_started_at = None

        # YAWN 触发(需武装):连续可靠张嘴达到 open_mouth_trigger
        if (
            self._yawn_armed
            and not self._yawn_active
            and self._mouth_open_started_at is not None
            and now - self._mouth_open_started_at >= cfg["open_mouth_trigger"] - _EPS
        ):
            self._yawn_triggered_at = self._mouth_open_started_at + cfg[
                "open_mouth_trigger"
            ]
            self._yawn_active = True
            self._yawn_armed = False

        # YAWN 保持与退出:保持期内闭嘴可累计;同时满足保持期结束与连续闭嘴
        # yawn_rearm_closed_mouth 才退出并重新武装。
        if self._yawn_active:
            if self._mouth_evidence == _MOUTH_CLOSED:
                if self._mouth_closed_since is None:
                    self._mouth_closed_since = now
            else:
                self._mouth_closed_since = None

            # YAWN 兜底:可靠睁眼且未检测到有效 open_mouth 的连续计时。
            # 冲突的 open_mouth/closed_mouth 仍有 open_mouth 证据，必须阻断恢复。
            if self._eye_evidence == _EYE_OPEN and self._best_conf["open_mouth"] == 0.0:
                if self._yawn_fallback_eye_since is None:
                    self._yawn_fallback_eye_since = now
            else:
                self._yawn_fallback_eye_since = None

            if (
                now - self._yawn_triggered_at >= cfg["yawn_min_hold"] - _EPS
                and (
                    (
                        self._mouth_closed_since is not None
                        and now - self._mouth_closed_since
                        >= cfg["yawn_rearm_closed_mouth"] - _EPS
                    )
                    or (
                        self._yawn_fallback_eye_since is not None
                        and now - self._yawn_fallback_eye_since
                        >= cfg["yawn_fallback_open_eye"] - _EPS
                    )
                )
            ):
                self._yawn_active = False
                self._yawn_armed = True
                self._yawn_triggered_at = None
                self._mouth_closed_since = None
                self._yawn_fallback_eye_since = None
        elif not self._yawn_armed:
            # 未武装(被 FATIGUE 清除):连续可靠闭嘴 yawn_rearm_closed_mouth 秒重新武装;
            # 任一非可靠闭嘴证据都清零重新武装计时。
            if self._mouth_evidence == _MOUTH_CLOSED:
                if self._rearm_closed_since is None:
                    self._rearm_closed_since = now
                if (
                    now - self._rearm_closed_since
                    >= cfg["yawn_rearm_closed_mouth"] - _EPS
                ):
                    self._yawn_armed = True
                    self._rearm_closed_since = None
            else:
                self._rearm_closed_since = None

    # -- 决策 ---------------------------------------------------------------

    def _eye_timeout(self, now) -> bool:
        last = self._last_reliable_eye_at
        return last is None or now - last >= self._cfg["unknown_timeout"] - _EPS

    def _decide_fatigue_latched(self, now):
        cfg = self._cfg
        # FATIGUE 期间:眼部证据连续不可靠达到 unknown_timeout -> UNKNOWN 并清锁存
        if self._eye_timeout(now):
            self._fatigue_latched = False
            self._eye_closed_started_at = None
            self._fatigue_recovery_since = None
            self._reset_yawn_after_fatigue()
            return FatigueState.UNKNOWN
        # 持续可靠睁眼 fatigue_recovery 秒才恢复 NORMAL
        if (
            self._fatigue_recovery_since is not None
            and now - self._fatigue_recovery_since >= cfg["fatigue_recovery"] - _EPS
        ):
            self._fatigue_latched = False
            self._eye_closed_started_at = None
            self._fatigue_recovery_since = None
            self._reset_yawn_after_fatigue()
            return FatigueState.NORMAL
        return FatigueState.FATIGUE

    def _fallback(self, now):
        """兜底:上一输出 NORMAL 且仍处于眼部宽限/闭眼未达阈值时保持 NORMAL。"""
        cfg = self._cfg
        in_grace = (
            self._last_reliable_eye_at is not None
            and now - self._last_reliable_eye_at <= cfg["eye_missing_grace"] + _EPS
        )
        closed_ongoing = (
            self._eye_closed_started_at is not None
            and now - self._eye_closed_started_at
            < cfg["closed_eye_trigger"] - _EPS
        )
        if self._state == FatigueState.NORMAL and (in_grace or closed_ongoing):
            return FatigueState.NORMAL
        return FatigueState.UNKNOWN

    def _clear_yawn(self):
        """FATIGUE 覆盖时:清除 YAWN 锁存、张嘴/闭嘴计时并置为未武装。"""
        self._yawn_active = False
        self._yawn_armed = False
        self._yawn_triggered_at = None
        self._mouth_open_started_at = None
        self._mouth_closed_since = None
        self._rearm_closed_since = None
        self._yawn_fallback_eye_since = None

    def _reset_yawn_after_fatigue(self):
        """FATIGUE 退出边界:丢弃可能开始于 FATIGUE 前/期间的嘴部与
        YAWN 证据(含旧张嘴计时,防止旧时长回放),并直接重新武装 YAWN,无需
        closed_mouth。恢复边界之后的连续可靠张嘴必须从零重新累计
        open_mouth_trigger 秒才能再次触发。"""
        self._yawn_active = False
        self._yawn_armed = True
        self._yawn_triggered_at = None
        self._mouth_open_started_at = None
        self._mouth_closed_since = None
        self._rearm_closed_since = None
        self._yawn_fallback_eye_since = None
        self._last_reliable_mouth_at = None

    # -- 证据评分 -----------------------------------------------------------

    def _evidence_score(self, state) -> int:
        if state == FatigueState.UNKNOWN:
            return 0
        target = {
            FatigueState.FATIGUE: "closed_eye",
            FatigueState.YAWN: "open_mouth",
            FatigueState.NORMAL: "open_eye",
        }[state]
        return int(round(self._best_conf.get(target, 0.0) * 100))
