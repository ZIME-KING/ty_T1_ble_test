# -*- coding: utf-8 -*-
"""离线冒烟测试(无需真实蓝牙)：
1) protocol 编解码/计算单测(含文档示例)
2) 用 FakeBLE 驱动 Controller 全流程: PASS / FAIL(设备返回1, 重试耗尽)
3) UI 离屏构建

运行: QT_QPA_PLATFORM=offscreen python run_smoke.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol as proto
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

_FAILS = []


def check(name: str, cond: bool, extra: str = ""):
    tag = "OK " if cond else "FAIL"
    print(f"[{tag}] {name} {extra}")
    if not cond:
        _FAILS.append(name)


# ---------------------------------------------------------------------- #
# 1) protocol
# ---------------------------------------------------------------------- #
def test_protocol():
    f = proto.TestFrame(voltage_mv=230000, current_ma=108, power_mw=24000,
                        iac=0xCAA1, uc=0xA576, pac=0xAC27)
    raw = bytes([0x01]) + struct.pack("<III", 230000, 108, 24000) \
        + struct.pack("<HHH", 0xCAA1, 0xA576, 0xAC27)
    assert len(raw) == 19
    got = proto.parse_test_frame(raw)
    check("parse_test_frame", got is not None and got == f)
    check("parse_test_frame 拒错长", proto.parse_test_frame(raw[:-1]) is None)
    check("parse_test_frame 拒cmd", proto.parse_test_frame(b"\x02" + raw[1:]) is None)

    cal = proto.build_cal_frame(0xCAA1, 0xA576, 0xAC27)
    check("build_cal_frame 长度/小端", cal == bytes([0x02, 0xA1, 0xCA, 0x76, 0xA5, 0x27, 0xAC]))
    check("clamp16 下限/上限", proto.clamp16(0) == 1 and proto.clamp16(70000) == 65535)

    check("parse_result_frame OK/FAIL",
          proto.parse_result_frame(b"\x02\x00") == 0 and
          proto.parse_result_frame(b"\x02\x01") == 1 and
          proto.parse_result_frame(b"\x01\x00") is None)

    # 文档§5示例: 220V/0.36A/79.2W 档
    new = proto.compute_new_coeffs(f, 220000, 360, 79200)
    check("compute 文档示例", new == (65535, 40516, 65535), f"got {new}")
    # 标准=0 通道保持原系数，其余照算
    new0 = proto.compute_new_coeffs(f, 220000, 0, 0)
    check("compute 通道跳过", new0 == (f.iac, 40516, f.pac), f"got {new0}")
    # 换算过程值: 字段齐全且最终 new 与 compute 一致
    det = proto.coeff_details(f, 220000, 0, 0)
    check("coeff_details 与 compute 一致",
          [d["new"] for d in det] == list(new0) and
          all(d["skipped"] == (d["std"] <= 0 or d["meas"] <= 0) for d in det),
          f"new={[d['new'] for d in det]} raw={[d['raw'] for d in det]}")


# ---------------------------------------------------------------------- #
# 2) FakeBLE + Controller
# ---------------------------------------------------------------------- #
class FakeBLE(QObject):
    message = Signal(str, str)
    status = Signal(str)
    ready = Signal()
    data = Signal(bytes)
    link_lost = Signal(str)
    scanning_changed = Signal(bool)
    devices_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "pass"
        self.writes: list[bytes] = []
        self.active = False          # 已连接
        self._scanning = False
        self.fake_key = "AA:BB:CC:DD:EE:FF"
        self._n = 0
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(60)
        self._scan_timer.timeout.connect(self._publish)
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._tick)

    # -- 接口(与 BleClient 对齐) --
    def start_scan(self) -> bool:
        if self._scanning:
            return False
        self._scanning = True
        self.scanning_changed.emit(True)
        self._scan_timer.start()
        return True

    def stop_scan(self):
        self._scanning = False
        self._scan_timer.stop()
        self.scanning_changed.emit(False)

    def clear_devices(self):
        pass  # Fake 设备在下一轮扫描时总会出现

    def device_list(self) -> list:
        return [{"key": self.fake_key, "name": "CALIB_DEV",
                 "addr": self.fake_key, "rssi": -45, "target": True}]

    def connect_selected(self, key: str) -> bool:
        if key != self.fake_key:
            return False
        self._teardown()   # 失败重连时设备仍处于连接态, 先复位模拟连接
        self.stop_scan()
        QTimer.singleShot(0, self._connected)
        return True

    def cancel(self):
        self._teardown()
        self.stop_scan()

    def disconnect_link(self):
        self._teardown()

    def connected_name(self) -> str:
        return f"FakeBLE {self.fake_key}"

    def write_cal_coeffs(self, iac, uc, pac) -> bool:
        if not self.active:
            return False
        self.writes.append(proto.build_cal_frame(iac, uc, pac))
        # 模拟设备: 回结果帧
        QTimer.singleShot(10, self._send_result)
        return True

    # -- 模拟设备行为 --
    def _publish(self):
        if not self._scanning:
            return
        self.devices_changed.emit(self.device_list())

    def _teardown(self):
        self.active = False
        self._n = 0
        self._frame_timer.stop()

    def _connected(self):
        if not self.active:
            self.active = True
            self.message.emit("FakeBLE 连接成功", "ok")
            self.status.emit("等待测试数据 ...")
            self.ready.emit()
            self._frame_timer.start(5)  # 加速: 每 5ms 一帧

    def _tick(self):
        if not self.active:
            return
        self._n += 1
        # 值贴近标准(220V/0.36A/79.2W)，保证新系数≈旧系数
        self.data.emit(self._frame())

    def _frame(self) -> bytes:
        v, i, p = 220000, 360, 79200
        return bytes([0x01]) + struct.pack("<III", v, i, p) \
            + struct.pack("<HHH", 10000, 20000, 30000)

    def _send_result(self):
        if not self.active:
            return
        if self.mode == "pass":
            self.data.emit(bytes([0x02, proto.RESULT_OK]))
            QTimer.singleShot(30, self._reset_sim)
        else:
            self.data.emit(bytes([0x02, proto.RESULT_FAIL]))

    def _reset_sim(self):
        if not self.active:
            return
        self._teardown()
        self.link_lost.emit("设备软复位断开")


class NullRecorder:
    def append(self, rec):
        print("  record:", rec["sn"], rec["result"], "note=", rec["note"],
              "iac_new=", rec["iac"], "attempt=", rec["attempt"])


def test_controller(app, mode: str) -> list:
    import controller as ctrlmod

    ble = FakeBLE()
    ble.mode = mode
    rec = NullRecorder()
    ctrl = ctrlmod.Controller(ble, rec)

    logs = []

    def on_log(t, k):
        logs.append((t, k))

    ctrl.logmsg.connect(on_log)
    done = []

    def on_finish(rec2: dict):
        done.append(rec2)
        if len(done) >= 1:
            ctrl.shutdown()
            QTimer.singleShot(0, app.quit)

    ctrl.finished.connect(on_finish)
    # 新流程: 开始扫描不需要 SN, SN 在确认写入时才提供
    ok = ctrl.start_unit("", 220.0, 0.36, 79.2, 3)
    check(f"[{mode}] start_unit(空SN) 接受并进入选择", ok and ctrl.phase == ctrlmod.CHOOSE)
    # 手动选择流程: 从扫描列表里取设备确认连接
    sel = ctrl.select_device(ble.fake_key)
    check(f"[{mode}] select_device 确认接受", sel)

    # 测量完成后等待人工确认：模拟操作员填写 SN 并点击【确认写入系数】
    confirm_poll = QTimer()
    confirm_poll.setInterval(40)

    def _maybe_confirm():
        if ctrl.phase == ctrlmod.WRITE:
            ctrl.confirm_write("SN-TEST-1")

    confirm_poll.timeout.connect(_maybe_confirm)
    confirm_poll.start()

    # 兜底 10s
    QTimer.singleShot(10000, app.quit)
    app.exec()

    if mode == "pass":
        check(f"[{mode}] 收到PASS", done and done[0]["result"] == "PASS",
              f"done={[d['result'] for d in done]}")
        check(f"[{mode}] 记录SN=确认时填写", done and done[0]["sn"] == "SN-TEST-1",
              f"sn={done and done[0]['sn']!r}")
        check(f"[{mode}] 写入系数正确", len(ble.writes) >= 1 and
              ble.writes[0] == bytes([0x02]) + struct.pack("<HHH", 10000, 20000, 30000),
              f"writes={ble.writes}")
    else:
        check(f"[{mode}] 重试耗尽判FAIL", done and done[0]["result"] == "FAIL",
              f"done={[d['result'] for d in done]}")
        check(f"[{mode}] 尝试次数={ctrlmod.MAX_ATTEMPTS}",
              done and done[0]["attempt"] == ctrlmod.MAX_ATTEMPTS)
        check(f"[{mode}] 重发次数≥{ctrlmod.MAX_ATTEMPTS}", len(ble.writes) >= ctrlmod.MAX_ATTEMPTS)
    return logs


# ---------------------------------------------------------------------- #
# 3) UI
# ---------------------------------------------------------------------- #
def test_ui(app):
    import controller as ctrlmod
    from ble import BleClient
    from recorder import Recorder
    from ui import MainWindow

    class NoBle(BleClient):
        """避免真实扫蓝牙；仅构建 UI。"""

    ble = NoBle()
    rec = Recorder(Path("calib_logs"))
    ctrl = ctrlmod.Controller(ble, rec)
    win = MainWindow(ctrl, rec, ble)
    win.show()
    app.processEvents()
    check("UI 构建", win.isVisible())
    # 模拟扫描列表更新进表格
    win._on_devices([{"key": "AA:BB:CC:DD:EE:FF", "name": "CALIB_DEV",
                      "addr": "AA:BB:CC:DD:EE:FF", "rssi": -45, "target": True}])
    app.processEvents()
    check("设备表格填充", win.dev_table.rowCount() == 1)
    win.close()
    app.processEvents()


if __name__ == "__main__":
    print("=== 1. protocol ===")
    test_protocol()
    app = QApplication(sys.argv)
    print("=== 2. 全流程 PASS ===")
    test_controller(app, "pass")
    print("=== 3. 全流程 FAIL(设备返回1) ===")
    test_controller(app, "fail")
    print("=== 4. UI 离屏构建 ===")
    test_ui(app)
    print()
    if _FAILS:
        print("存在失败项:", _FAILS)
        sys.exit(1)
    print("ALL TESTS PASS")
