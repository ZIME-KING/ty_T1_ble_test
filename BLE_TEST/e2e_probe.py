# -*- coding: utf-8 -*-
"""E2E 探针(临时)：真实 Controller + bleak BleClient 全流程, 仅差界面点击。

流程 = 用户点击"开始/确认连接"的等价调用：
  start_unit(SN) -> CHOOSE -> 自动选中目标 -> connect_selected -> ready(订阅)
  -> MEASURE 收帧 -> 收满后 abort 结束(不下发系数, 避免改动实机校准)。
同时每 1s 输出 ALIVE 心跳，若主线程被 BLE 阻塞(旧 QtBluetooth 死锁)心跳会停止。
"""
import os
import sys
import threading
import time

HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib_host")
sys.path.insert(0, HOST)

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

app = QCoreApplication(sys.argv)
T0 = time.time()


def out(*a):
    print(f"[{time.time() - T0:7.1f}s]", *a, flush=True)


def bail(code, why=""):
    out(f"EXIT code={code} {why}")
    os._exit(code)


threading.Timer(75, lambda: bail(90, "GLOBAL_TIMEOUT")).start()

from ble import BleClient  # noqa: E402
from controller import Controller  # noqa: E402
from recorder import Recorder  # noqa: E402

ble = BleClient()
ctrl = Controller(ble, Recorder())

ctrl.logmsg.connect(lambda t, k: out(f"[{k}] {t}"))
ctrl.status.connect(lambda s: out(f"[status] {s}"))
ble.message.connect(lambda t, k: out(f"[ble.{k}] {t}"))
ble.ready.connect(lambda: out("[ble] READY"))
ble.link_lost.connect(lambda r: out(f"[ble.link_lost] {r}"))
ble.scanning_changed.connect(lambda b: out(f"[ble.scanning] {b}"))

state = {"started": False, "selected": False, "idle_before_ready": False}
watch_no_ready = [False]


def _no_ready():
    if not watch_no_ready[0]:
        watch_no_ready[0] = True
        bail(91, "NO_READY_IN_45S")


def tick():
    # 1) 开始
    if not state["started"]:
        ok = ctrl.start_unit("E2E-PROBE", 220.0, 0.36, 79.2, 8)
        out(f"start_unit -> {ok}")
        state["started"] = True
        threading.Timer(45, _no_ready).start()
        QTimer.singleShot(500, tick)
        return
    # 2) 等待已收到 >=3 帧(MEASURE 阶段、ready 后)
    if ctrl.busy and getattr(ctrl, "_frames", None) and len(ctrl._frames) >= 3:
        out(f"收到 {len(ctrl._frames)} 帧, abort 结束")
        ctrl.abort()
        QTimer.singleShot(800, lambda: bail(0, "PASS"))
        return
    # 3) CHOOSE: 选中目标设备(等价 UI 点"确认连接所选设备")
    if ctrl.busy and ctrl.phase == "选择设备":
        if not state["selected"]:
            for d in ble.device_list():
                if d["target"]:
                    out(f"选中目标 {d}")
                    state["selected"] = True
                    ok = ctrl.select_device(d["key"])
                    out(f"select_device -> {ok} (等价点确认连接)")
                    if not ok:
                        bail(3, "select_device False")
                    break
            else:
                out("目标尚未出现, 继续等扫描 ...")
    QTimer.singleShot(400, tick)


ctrl.finished.connect(lambda r: out(f"[finished] {r['result']} {r['note']}"))

hb_n = [0]
hb = QTimer()
def heartbeat():
    hb_n[0] += 1
    out(f"ALIVE {hb_n[0]}  phase={ctrl.phase}")
hb.timeout.connect(heartbeat)
hb.start(1000)

QTimer.singleShot(100, tick)
rc = app.exec()
out(f"exec returned {rc}")
sys.exit(rc)
