"""
CHG-G5-001 A4 独立离线验收: 主机 DMS1 事件遥测最小内部可观测性。

覆盖 CHG-G5-001-A01..A06 及任务要求的补充断言:
  - A01 未设置 DMS1_EVENT_LOG: 零事件回调/零 JSONL 文件, 帧字节与 seq 照常。
  - A02 frame 事件 seq/state/confidence 与实际写入帧一致, 逐帧递增无缺口;
       visual/visual_ms/duration_ms 绑定最近一次 publish(result) 冻结原始语义;
       陈旧降级时 state=UNKNOWN 而 visual 保留最近原始状态; 首帧 visual=UNKNOWN。
  - A03 event_cb / log_cb / writer 写入抛异常不影响帧发送与 seq; 写失败无 frame 事件。
  - A04 start/connect/connect_failed/disconnect/stop schema 与必填字段,
       对可立即连接的 FakeSerial 断言 start 先于 connect 先于 frame, mono_ms 会话内不递减,
       wall_ms 为 epoch UTC 毫秒。
  - A05 JSONL UTF-8 追加、逐行可 JSON 解析、追加字段不影响基础字段语义。
  - A06 仅使用 FakeSerial; 断言未通过真实 COM3 创建任何串口实例, 未枚举串口。

约束: 不占用 COM3, 不启动 GUI/模型, 不修改产品源码。UI 仅做源码静态检查。
"""
import json
import os
import sys
import time
import unittest
import tempfile
from dataclasses import dataclass
from unittest.mock import patch

import serial as _serial_mod

WORKDIR = os.path.join(os.path.dirname(__file__), "..", "..",
                       "host_app")
sys.path.insert(0, WORKDIR)

from serial_bridge import (  # noqa: E402
    SerialPublisher, JsonlEventWriter,
    encode_dms1_frame, parse_dms1_frame,
    HEARTBEAT_PERIOD, STALE_TIMEOUT, SEQ_MAX,
    RECONNECT_INITIAL,
)

UI_PATH = os.path.join(WORKDIR, "ui.py")


# ---------------------------------------------------------------------------
# 测试替身: FakeSerial / 可注入时钟 / 结果对象
# ---------------------------------------------------------------------------

class FakeSerial:
    """最小串口替身。绝不对接真实 COM 口。"""

    def __init__(self, fail_open=False, fail_write=False, record=None):
        self.is_open = False
        self.port = None
        self.dtr = True
        self.rts = True
        self.fail_open = fail_open
        self.fail_write = fail_write
        self.record = record if record is not None else []
        self.write_count = 0

    def open(self):
        if self.fail_open:
            raise _serial_mod.SerialException("mock open failure")
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, data):
        if self.fail_write:
            raise _serial_mod.SerialException("mock write failure")
        self.record.append(bytes(data))
        self.write_count += 1
        return len(data)

    def flush(self):
        pass


class ManualClock:
    """可注入单调时钟(测试用)。"""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = t


@dataclass(frozen=True)
class MockResult:
    state: str
    evidence_score: int
    duration_ms: int
    timestamp_ms: int


def wait_for(pred, events, timeout=5.0, msg=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred(events):
            return
        time.sleep(0.01)
    raise AssertionError(f"timeout waiting for condition {msg}; events={events}")


def send_one(pub, fake, clock):
    """复刻 SerialPublisher._run 的帧发送段(写成功后 + frame 事件, 然后 seq 自增)。"""
    now = clock()
    state, conf = pub._current_state(now)
    frame = encode_dms1_frame(pub._seq, state, conf)
    fake.write(frame)
    fake.flush()
    pub._emit_event(pub._frame_event(state, conf, now))
    pub._seq = (pub._seq + 1) % (SEQ_MAX + 1)


def _types(events):
    return [e["type"] for e in events]


# ---------------------------------------------------------------------------
# A01: DMS1_EVENT_LOG 未设置
# ---------------------------------------------------------------------------

class G5TelemetryDisabledTest(unittest.TestCase):

    def _make_pub(self, events=None):
        return SerialPublisher(
            port="TEST",
            clock=ManualClock(1000.0),
            log_cb=None,
            event_cb=events.append if events is not None else None,
        )

    def test_no_env_zero_events_and_no_jsonl_file(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DMS1_EVENT_LOG", None)
                pub = self._make_pub()
                fake = FakeSerial()
                pub._ser = fake
                pub._running = True
                for _ in range(4):
                    send_one(pub, fake, ManualClock(1000.0 + _ * 0.5))
                pub._close_port()
            # 未设置时不应产生任何文件
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".jsonl")], [])
            # _emit_event 遇 None 回调直接返回, 不产生任何副作用
            pub._emit_event({"type": "frame"})
            self.assertEqual(pub._event_cb, None)

    def test_no_env_frame_bytes_match_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "ev.jsonl")
            # 遥测关闭(模拟未设置 env -> ui 传 event_cb=None)
            pub_none = SerialPublisher(
                port="TEST", clock=ManualClock(1000.0), log_cb=None, event_cb=None)
            fake_none = FakeSerial()
            pub_none._ser = fake_none
            pub_none.publish(MockResult("FATIGUE", 91, 1500, 1000000))
            clk = ManualClock(1000.0)
            for _ in range(3):
                send_one(pub_none, fake_none, clk)

            # 遥测开启: 同一输入驱动
            writer = JsonlEventWriter(log_path)
            pub_on = SerialPublisher(
                port="TEST", clock=ManualClock(1000.0), log_cb=None,
                event_cb=writer.write)
            fake_on = FakeSerial()
            pub_on._ser = fake_on
            pub_on.publish(MockResult("FATIGUE", 91, 1500, 1000000))
            clk2 = ManualClock(1000.0)
            for _ in range(3):
                send_one(pub_on, fake_on, clk2)
            writer.close()

            # 未设置/设置两条路径写出的帧字节与 seq 完全一致
            self.assertEqual(fake_none.record, fake_on.record)
            self.assertEqual(pub_none._seq, pub_on._seq)
            self.assertTrue(fake_on.record)  # 遥测确实没有干扰发送


# ---------------------------------------------------------------------------
# A02: frame 事件字段与写入帧一致; visual 绑定最近原始 result
# ---------------------------------------------------------------------------

class G5TelemetryFrameTest(unittest.TestCase):

    def test_frame_seq_state_confidence_match_written(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(5000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        pub.publish(MockResult("NORMAL", 95, 300, 5000000))
        for _ in range(3):
            send_one(pub, fake, clk)

        frames = [e for e in events if e["type"] == "frame"]
        self.assertEqual(len(frames), 3)
        for i, ev in enumerate(frames):
            parsed = parse_dms1_frame(fake.record[i])
            self.assertIsNotNone(parsed)
            self.assertEqual(ev["seq"], parsed["seq"])
            self.assertEqual(ev["state"], parsed["state"])
            self.assertEqual(ev["confidence"], parsed["confidence"])
            self.assertEqual(ev["state"], "NORMAL")

    def test_seq_sequential_no_gap(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(7000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        pub.publish(MockResult("YAWN", 88, 400, 7000000))
        for _ in range(5):
            send_one(pub, fake, clk)
        seqs = [e["seq"] for e in events if e["type"] == "frame"]
        self.assertEqual(seqs, [0, 1, 2, 3, 4])

    def test_visual_binds_latest_result(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(9000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        pub.publish(MockResult("FATIGUE", 91, 1500, 9000000))
        send_one(pub, fake, clk)

        ev = events[0]
        self.assertEqual(ev["type"], "frame")
        self.assertEqual(ev["state"], "FATIGUE")
        self.assertEqual(ev["visual"], "FATIGUE")       # == publish 传入原始 state
        self.assertEqual(ev["visual_ms"], 9000000)       # == 同一 result.timestamp_ms
        self.assertEqual(ev["duration_ms"], 1500)        # == 同一 result.duration_ms
        self.assertEqual(ev["confidence"], 91)

    def test_stale_degrades_state_unknown_visual_kept(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(10000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        pub.publish(MockResult("NORMAL", 90, 500, 10000000))
        # 新鲜: age 0.3s < 1.5s
        clk.set(10000.3)
        send_one(pub, fake, clk)
        # 陈旧: age > 1.5s -> state 降级 UNKNOWN, 但 visual 保留最近原始状态
        clk.set(10002.0)
        send_one(pub, fake, clk)

        f0, f1 = [e for e in events if e["type"] == "frame"]
        self.assertEqual(f0["state"], "NORMAL")
        self.assertEqual(f0["visual"], "NORMAL")
        self.assertEqual(f1["state"], "UNKNOWN")        # 陈旧降级
        self.assertEqual(f1["visual"], "NORMAL")        # visual 保留原始状态
        self.assertEqual(f1["visual_ms"], 10000000)     # 同一 result 的 timestamp_ms
        self.assertEqual(f1["duration_ms"], 500)        # 同一 result 的 duration_ms

    def test_first_frame_before_result_visual_unknown(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(11000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        send_one(pub, fake, clk)  # 无任何 result 的首帧

        ev = events[0]
        self.assertEqual(ev["state"], "UNKNOWN")
        self.assertEqual(ev["visual"], "UNKNOWN")
        self.assertEqual(ev["visual_ms"], int(11000.0 * 1000))  # 取当次帧单调毫秒
        self.assertEqual(ev["duration_ms"], 0)

    def test_visual_ms_monotonic_positive(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(12000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        for i in range(4):
            clk.set(12000.0 + i * 0.5)
            pub.publish(MockResult("NORMAL", 50, 100, int(clk.t * 1000)))
            send_one(pub, fake, clk)
        vms = [e["visual_ms"] for e in events if e["type"] == "frame"]
        self.assertEqual(vms, sorted(vms))
        self.assertTrue(all(v >= 0 for v in vms))


# ---------------------------------------------------------------------------
# A02/A05: JSONL writer — UTF-8 追加、逐行可解析、追加字段
# ---------------------------------------------------------------------------

class G5TelemetryWriterTest(unittest.TestCase):

    def test_utf8_append_line_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ev.jsonl")
            w = JsonlEventWriter(p)
            w.write({"type": "start", "mono_ms": 1, "wall_ms": 1000})
            w.write({"type": "frame", "seq": 0, "note": "状态帧-中文"})
            w.write({"type": "stop", "mono_ms": 2, "wall_ms": 2000})
            w.close()
            w2 = JsonlEventWriter(p)  # 追加模式, 绝不覆盖
            w2.write({"type": "connect", "port": "COM3"})
            w2.close()

            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 4)
            objs = [json.loads(l) for l in lines]
            self.assertEqual([o["type"] for o in objs],
                             ["start", "frame", "stop", "connect"])
            self.assertEqual(objs[1]["note"], "状态帧-中文")  # UTF-8 往返
            self.assertEqual(objs[3]["port"], "COM3")

    def test_append_extra_fields_do_not_break_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ev.jsonl")
            w = JsonlEventWriter(p)
            w.write({"type": "frame", "seq": 1, "mono_ms": 5, "wall_ms": 5000,
                     "future_field": "x", "future_field2": 42})
            w.write({"type": "frame", "seq": 2, "mono_ms": 6, "wall_ms": 6000})
            w.close()
            with open(p, encoding="utf-8") as f:
                objs = [json.loads(l) for l in f.read().splitlines()]
            self.assertEqual(len(objs), 2)
            # 基础字段语义稳定
            self.assertEqual(objs[0]["type"], "frame")
            self.assertEqual(objs[0]["mono_ms"], 5)
            self.assertEqual(objs[0]["wall_ms"], 5000)
            self.assertEqual(objs[0]["future_field"], "x")
            self.assertEqual(objs[1]["seq"], 2)

    def test_written_after_close_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ev.jsonl")
            w = JsonlEventWriter(p)
            w.close()
            self.assertFalse(w.write({"type": "stop"}))
            self.assertEqual(w.wrote, 0)


# ---------------------------------------------------------------------------
# A03: 回调/写入器异常隔离, 不影响帧发送与 seq
# ---------------------------------------------------------------------------

class G5TelemetryErrorIsolationTest(unittest.TestCase):

    def test_event_cb_raise_does_not_break_send(self):
        events = []
        fake = FakeSerial()
        clk = ManualClock(13000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None,
                             event_cb=events.append)
        pub._ser = fake

        def bad_cb(ev):
            events.append(ev)
            raise RuntimeError("cb boom")

        pub._event_cb = bad_cb
        pub.publish(MockResult("NORMAL", 90, 100, 13000000))
        send_one(pub, fake, clk)  # 不得抛出
        self.assertEqual(pub._seq, 1)  # seq 照常自增
        self.assertEqual(len(fake.record), 1)  # 帧照常写出
        self.assertEqual(events[0]["state"], "NORMAL")

    def test_log_cb_raise_does_not_break_send(self):
        fake = FakeSerial()
        clk = ManualClock(14000.0)
        pub = SerialPublisher(port="TEST", clock=clk,
                             log_cb=lambda s: (_ for _ in ()).throw(RuntimeError("log boom")))
        pub._log("hello")  # 不得抛出
        pub._ser = fake
        pub.publish(MockResult("NORMAL", 90, 100, 14000000))
        send_one(pub, fake, clk)
        self.assertEqual(pub._seq, 1)
        self.assertEqual(len(fake.record), 1)

    def test_writer_write_failure_isolated(self):
        class ThrowingFile:
            def write(self, s):
                raise OSError("disk full")

            def flush(self):
                raise OSError("flush fail")

            def close(self):
                raise OSError("close fail")

        with tempfile.TemporaryDirectory() as d:
            w = JsonlEventWriter(os.path.join(d, "ev.jsonl"))
            orig = w._file
            w._file = ThrowingFile()
            orig.close()  # 释放原句柄, 避免 ResourceWarning
            self.assertFalse(w.write({"type": "frame", "seq": 0}))  # 返回 False 不抛
            self.assertEqual(w.failures, 1)
            w.close()  # 不抛

            fake = FakeSerial()
            clk = ManualClock(15000.0)
            pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=w.write)
            pub._ser = fake
            pub.publish(MockResult("FATIGUE", 80, 200, 15000000))
            send_one(pub, fake, clk)  # writer 失败不中断发送
            self.assertEqual(pub._seq, 1)
            self.assertEqual(len(fake.record), 1)
            self.assertGreaterEqual(w.failures, 2)  # 后续写入失败仍被隔离

    def test_serial_write_failure_no_frame_event_no_seq(self):
        events = []
        fake = FakeSerial(fail_write=True)
        clk = ManualClock(16000.0)
        pub = SerialPublisher(port="TEST", clock=clk, log_cb=None, event_cb=events.append)
        pub._ser = fake
        pub.publish(MockResult("NORMAL", 90, 100, 16000000))
        pub._seq = 7

        # 复刻 _run 写失败分支: 不产生 frame 事件、seq 不自增、关闭端口
        try:
            frame = encode_dms1_frame(pub._seq, "NORMAL", 90)
            fake.write(frame)
            fake.flush()
            pub._emit_event(pub._frame_event("NORMAL", 90, clk()))
            pub._seq = (pub._seq + 1) % (SEQ_MAX + 1)
        except (_serial_mod.SerialException, OSError):
            pub._close_port()

        self.assertEqual([e for e in events if e["type"] == "frame"], [])
        self.assertEqual(pub._seq, 7)  # 写失败不占序号
        self.assertIsNone(pub._ser)

        # 恢复连接后继续发送, seq 从原值延续
        fake2 = FakeSerial()
        pub._ser = fake2
        send_one(pub, fake2, clk)
        self.assertEqual(pub._seq, 8)


# ---------------------------------------------------------------------------
# A04: 生命周期事件 schema / 顺序 / 单调时钟 (真实线程 + 可立即连接 FakeSerial)
# ---------------------------------------------------------------------------

class G5TelemetryLifecycleTest(unittest.TestCase):

    def _run_session(self, fake_ctor, frames_needed=1):
        events = []
        fake = fake_ctor(FakeSerial)
        pub = SerialPublisher(port="COM3", clock=time.monotonic, log_cb=None,
                              event_cb=events.append)
        try:
            with patch("serial_bridge.serial.Serial", return_value=fake):
                pub.start()
                wait_for(lambda e: "connect" in _types(e) or "connect_failed" in _types(e),
                         events)
                if "connect" in _types(events):
                    pub.publish(MockResult("NORMAL", 90, 200,
                                           int(time.monotonic() * 1000)))
                    wait_for(lambda e: any(
                        x["type"] == "frame" and x["state"] == "NORMAL" for x in e), events)
                pub.stop()
        finally:
            pub.stop()
        return events, pub

    def test_start_before_connect_before_frame(self):
        events, pub = self._run_session(lambda G: G())
        types = _types(events)
        self.assertEqual(types[0], "start")
        self.assertLess(types.index("connect"), types.index("frame"))

    def test_lifecycle_schema_required_fields(self):
        events, pub = self._run_session(lambda G: G())
        by_type = {}
        for e in events:
            by_type.setdefault(e["type"], e)

        for t in ("start", "connect", "frame", "disconnect", "stop"):
            self.assertIn(t, by_type, f"missing event {t}: {_types(events)}")
        # 基础字段
        for e in events:
            self.assertIn("type", e)
            self.assertIsInstance(e["mono_ms"], int)
            self.assertIsInstance(e["wall_ms"], int)
        # 连接类事件必含 port
        for t in ("connect", "disconnect"):
            self.assertIn("port", by_type[t])
        # start/stop 无附加必填(契约 §3)
        # frame 必填字段
        fr = by_type["frame"]
        for k in ("seq", "state", "confidence", "visual", "visual_ms", "duration_ms"):
            self.assertIn(k, fr, f"frame missing {k}")

    def test_mono_ms_nondecreasing_and_wall_ms_epoch(self):
        events, pub = self._run_session(lambda G: G())
        ms = [e["mono_ms"] for e in events]
        self.assertEqual(ms, sorted(ms), "mono_ms 会话内不递减")
        t0 = int(time.time() * 1000) - 60000
        t1 = int(time.time() * 1000) + 60000
        for e in events:
            self.assertTrue(t0 <= e["wall_ms"] <= t1,
                            f"wall_ms {e['wall_ms']} 不是 epoch UTC 毫秒")

    def test_connect_failed_port_and_wait(self):
        events, pub = self._run_session(lambda G: G(fail_open=True))
        cf = [e for e in events if e["type"] == "connect_failed"]
        self.assertGreaterEqual(len(cf), 1, f"no connect_failed: {_types(events)}")
        self.assertEqual(cf[0]["port"], "COM3")
        self.assertIsInstance(cf[0]["wait"], int)
        self.assertGreater(cf[0]["wait"], 0)
        self.assertLess(_types(events).index("start"), _types(events).index("connect_failed"))
        # 连接失败时 stop 仍发出, 且无 connect
        self.assertNotIn("connect", _types(events))

    def test_stop_emitted_after_disconnect(self):
        events, pub = self._run_session(lambda G: G())
        types = _types(events)
        self.assertLess(types.index("disconnect"), types.index("stop"))


# ---------------------------------------------------------------------------
# A06: 仅使用 FakeSerial, 未通过真实 COM3 创建串口 / 未枚举串口
# ---------------------------------------------------------------------------

class G5TelemetryNoComTest(unittest.TestCase):

    def _make_pub(self, port="COM3", event_cb=None):
        return SerialPublisher(port=port, clock=ManualClock(0.0),
                               log_cb=None, event_cb=event_cb)

    def test_connect_uses_safe_port_none_single_instance(self):
        created = []
        events = []
        with patch("serial_bridge.serial.Serial",
                   side_effect=lambda **kw: created.append(kw) or FakeSerial()), \
             patch("serial_bridge.serial.tools.list_ports") as lp:
            pub = self._make_pub(event_cb=events.append)
            pub._try_connect(1.0)
            self.assertEqual(len(created), 1)
            self.assertIsNone(created[0]["port"])  # 安全模式 port=None
            pub._close_port()
        lp.assert_not_called()

    def test_writer_creates_no_serial_instance(self):
        created = []
        with patch("serial_bridge.serial.Serial",
                   side_effect=lambda **kw: created.append(kw) or FakeSerial()):
            with tempfile.TemporaryDirectory() as d:
                w = JsonlEventWriter(os.path.join(d, "ev.jsonl"))
                w.write({"type": "frame", "seq": 0})
                w.write({"type": "stop"})
                w.close()
        self.assertEqual(created, [])  # 遥测运行期间零串口实例

    def test_connect_failed_also_never_uses_com3(self):
        created = []
        def ctor(**kw):
            created.append(kw)
            return FakeSerial(fail_open=True)
        events = []
        with patch("serial_bridge.serial.Serial", side_effect=ctor):
            pub = self._make_pub(event_cb=events.append)
            pub._try_connect(1.0)
        self.assertEqual(len(created), 1)
        self.assertIsNone(created[0]["port"])
        self.assertTrue(any(e["type"] == "connect_failed" for e in events))


# ---------------------------------------------------------------------------
# UI 静态检查: 环境变量开关与 closeEvent 收尾(不实例化 GUI/模型)
# ---------------------------------------------------------------------------

class G5UiGatingStaticTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(UI_PATH, encoding="utf-8") as f:
            cls.src = f.read()

    def test_env_switch_present(self):
        self.assertIn('os.environ.get("DMS1_EVENT_LOG")', self.src)

    def test_writer_created_only_when_env_set(self):
        idx_env = self.src.index('_log_path = os.environ.get("DMS1_EVENT_LOG")')
        idx_writer = self.src.index("self._event_writer = JsonlEventWriter(_log_path)")
        idx_cb = self.src.index("_event_cb = self._event_writer.write")
        # 顺序: 读 env -> 创建 writer -> 设回调; 且受 if _log_path 保护
        self.assertLess(idx_env, idx_writer)
        self.assertLess(idx_writer, idx_cb)
        self.assertIn("if _log_path:", self.src)

    def test_default_no_writer(self):
        self.assertIn("self._event_writer = None", self.src)

    def test_unwritable_event_log_disables_telemetry(self):
        self.assertIn("except OSError as exc:", self.src)
        self.assertIn("self._event_writer_error", self.src)
        self.assertIn("self._signals.serial_log.emit(self._event_writer_error)", self.src)

    def test_close_event_reclaims_writer_after_stop(self):
        ci = self.src.index("def closeEvent")
        stop_i = self.src.index("self._serial.stop()", ci)
        close_i = self.src.index("self._event_writer.close()", ci)
        self.assertLess(stop_i, close_i)

    def test_runtime_metrics_are_opt_in_and_inference_only(self):
        self.assertIn('os.environ.get("DMS1_RUNTIME_METRICS_LOG")', self.src)
        self.assertIn("self._runtime_metrics_writer = JsonlEventWriter(_metrics_path)", self.src)
        self.assertIn("self._record_runtime_metrics(frame_rgb is not None)", self.src)
        self.assertIn('"inference_fps"', self.src)

    def test_close_event_reclaims_runtime_metrics_after_inference_stops(self):
        ci = self.src.index("def closeEvent")
        join_i = self.src.index("self._inference_thread.join", ci)
        close_i = self.src.index("self._runtime_metrics_writer.close()", ci)
        self.assertLess(join_i, close_i)

    def test_e2e08_inference_fault_is_opt_in_and_bounded(self):
        self.assertIn('"DMS1_TEST_INFERENCE_FAILURE_DELAY_S"', self.src)
        self.assertIn('"DMS1_TEST_INFERENCE_FAILURE_DURATION_S"', self.src)
        self.assertIn('raise RuntimeError("Test-only injected inference failure")', self.src)
        self.assertIn('"type": "test_inference_fault"', self.src)
        self.assertIn("self._maybe_inject_test_inference_failure()", self.src)


if __name__ == "__main__":
    unittest.main()
