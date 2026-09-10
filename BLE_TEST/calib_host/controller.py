# -*- coding: utf-8 -*-
"""校准流程控制器(状态机)：一台接一台批量校准。

SN(可后填/可预填) -> 扫描显示所有BLE设备(人工点选) -> 连接/订阅 -> 收N帧平均 ->
计算新系数 -> 确认写入时必须填写SN -> 下发(7B) -> 收结果(2B) ->
设备软复位断开 -> 记录 -> 等待下一台。

连接失败自动重连同一目标，超过尝试次数判 FAIL；各阶段看门狗兜底。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

import protocol as proto

IDLE = "待机"
CHOOSE = "选择设备"
CONNECT = "连接"
MEASURE = "采集数据"
WRITE = "下发系数"
RESULT = "等待结果"
PASS_WAIT = "等待复位"

MAX_ATTEMPTS = 3          # 同一台最多尝试次数(含首试)
DROP_FIRST_FRAMES = 1     # 订阅后丢弃前几帧(避开开机瞬间)

WATCH_CONNECT = 25000     # 连接/发现服务超时
WATCH_MEASURE = 15000     # 测量阶段连续无帧
WATCH_RESULT = 6000       # 下发后等待结果
WATCH_PASS = 6000         # 成功后等待设备复位断开


class Controller(QObject):
    status = Signal(str)          # 状态大字
    logmsg = Signal(str, str)     # (文本, kind)  info/ok/err
    live = Signal(dict)           # 实时测量刷新
    finished = Signal(dict)       # 单台完成记录

    def __init__(self, ble, recorder, parent: QObject | None = None):
        super().__init__(parent)
        self.ble = ble
        self.rec = recorder
        self._phase = IDLE
        self._busy = False
        self._sn = ""
        self._attempt = 0
        self._avg_n = 8
        self._stds = {}
        self._frames: list[proto.TestFrame] = []
        self._to_drop = DROP_FIRST_FRAMES
        self._pending = None
        self._target_key: str | None = None
        self._watch = QTimer(self)
        self._watch.setSingleShot(True)
        self._watch.timeout.connect(self._on_watchdog)

        ble.message.connect(self._on_msg)
        ble.ready.connect(self._on_ready)
        ble.data.connect(self._on_data)
        ble.link_lost.connect(self._on_link_lost)

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def phase(self) -> str:
        return self._phase

    def start_unit(self, sn: str, std_v_v: float, std_a_a: float,
                   std_p_w: float, avg_n: int) -> bool:
        """开始校准一台：进入扫描选择设备。忙则忽略；SN 可留空(确认写入时必填)。"""
        if self._busy:
            return False
        self._sn = (sn or "").strip()
        self._attempt = 0
        self._avg_n = max(3, int(avg_n))
        self._stds = {
            "mv": round(std_v_v * 1000),
            "ma": round(std_a_a * 1000),
            "mw": round(std_p_w * 1000),
        }
        head = f"SN={self._sn} " if self._sn else ""
        self.logmsg.emit(
            f"{head}开始 | 标准 V={std_v_v:g} A={std_a_a:g} W={std_p_w:g} | "
            f"平均帧数={self._avg_n} | 请在下表选择设备", "info")
        self._busy = True
        self._target_key = None
        self._frames.clear()
        self._to_drop = DROP_FIRST_FRAMES
        self.ble.clear_devices()
        self._set_phase(CHOOSE, 0)
        self.ble.start_scan()
        return True

    def select_device(self, key: str) -> bool:
        """人工确认连接某个已扫到的设备。"""
        if not self._busy or self._phase not in (CHOOSE, CONNECT):
            return False
        self._target_key = key
        ok = self.ble.connect_selected(key)
        if ok:
            self._set_phase(CONNECT, WATCH_CONNECT)
            self.logmsg.emit(f"目标设备: {self.ble.connected_name()}", "info")
        else:
            self.logmsg.emit("连接未开始，请重选或重新扫描", "err")
            self._set_phase(CHOOSE, 0)
            self.ble.start_scan()
        return ok

    def rescan(self):
        """选择阶段清空列表重新扫描。"""
        if not self._busy or self._phase != CHOOSE:
            return
        self.logmsg.emit("重新扫描 ...", "info")
        self.ble.clear_devices()
        self.ble.stop_scan()
        self.ble.start_scan()

    def abort(self):
        """中止当前台(不计结果)。"""
        if not self._busy:
            return
        self.logmsg.emit("用户中止", "err")
        self._reset_to_idle()

    def shutdown(self):
        self._watch.stop()
        self.ble.cancel()

    # ------------------------------------------------------------------ #
    # 阶段控制
    # ------------------------------------------------------------------ #
    def _set_phase(self, phase: str, watch_ms: int):
        self._phase = phase
        self.status.emit(phase)
        if watch_ms > 0:
            self._watch.start(watch_ms)
        else:
            self._watch.stop()

    def _retry_or_fail(self, reason: str):
        """当前台失败；未超次数则重连同一目标，否则判 FAIL。"""
        self._attempt += 1
        self.logmsg.emit(f"{reason} (尝试 {self._attempt}/{MAX_ATTEMPTS})", "err")
        if self._attempt >= MAX_ATTEMPTS:
            self._finish("FAIL", reason)
            return
        if self._target_key is None:
            self._set_phase(CHOOSE, 0)
            self.logmsg.emit("请重新扫描并选择设备", "err")
            self.ble.start_scan()
            return
        self._set_phase(CONNECT, WATCH_CONNECT)
        if not self.ble.connect_selected(self._target_key):
            self._retry_or_fail("重连未开始")

    def _on_watchdog(self):
        if not self._busy:
            return
        ph = self._phase
        if ph == CONNECT:
            self._retry_or_fail("连接超时")
        elif ph == MEASURE:
            self._retry_or_fail("测量数据中断")
        elif ph == RESULT:
            self._retry_or_fail("等待校准结果超时")
        elif ph == PASS_WAIT:
            self.logmsg.emit("未收到断开信号，主动断开并判成功", "info")
            self.ble.cancel()
            self._finish("PASS", "OK(主动断开)")

    # ------------------------------------------------------------------ #
    # BLE 事件
    # ------------------------------------------------------------------ #
    def _on_msg(self, text: str, kind: str):
        self.logmsg.emit(text, kind)
        if kind == "err" and self._busy and self._phase == CONNECT:
            self._retry_or_fail(text)

    def _on_ready(self):
        if not self._busy:
            return
        self._frames.clear()
        self._to_drop = DROP_FIRST_FRAMES
        self._set_phase(MEASURE, WATCH_MEASURE)

    def _on_data(self, raw: bytes):
        if not self._busy or self._phase not in (MEASURE, RESULT):
            return
        if self._phase == RESULT:
            r = proto.parse_result_frame(raw)
            if r is None:
                return  # 忽略残留测试帧
            if r == proto.RESULT_OK:
                self.logmsg.emit("收到结果: 成功", "ok")
                self._set_phase(PASS_WAIT, WATCH_PASS)
            else:
                self._retry_or_fail("设备返回校准失败(result=1)")
            return
        f = proto.parse_test_frame(raw)
        if f is None:
            return
        if self._to_drop > 0:
            self._to_drop -= 1
            return
        self._frames.append(f)
        keep = max(self._avg_n * 2, self._avg_n + 5)
        if len(self._frames) > keep:
            del self._frames[:len(self._frames) - keep]
        self._watch.start(WATCH_MEASURE)  # 有帧则续期
        self._emit_live()
        if len(self._frames) >= self._avg_n:
            self._calibrate()

    def _on_link_lost(self, reason: str):
        if not self._busy:
            return
        self.logmsg.emit(f"链路断开: {reason}", "info")
        if self._phase == PASS_WAIT:
            self._finish("PASS", "OK")
        elif self._phase in (RESULT, MEASURE, WRITE, CONNECT):
            self._retry_or_fail("连接中断")

    # ------------------------------------------------------------------ #
    # 校准计算
    # ------------------------------------------------------------------ #
    def _mean_frame(self) -> proto.TestFrame:
        win = self._frames[-self._avg_n:]
        n = len(win)
        last = win[-1]
        return proto.TestFrame(
            voltage_mv=sum(f.voltage_mv for f in win) // n,
            current_ma=sum(f.current_ma for f in win) // n,
            power_mw=sum(f.power_mw for f in win) // n,
            iac=last.iac, uc=last.uc, pac=last.pac,
        )

    def confirm_write(self, sn: str = "") -> bool:
        """人工确认后真正下发系数(替代原来的自动下发)。

        此刻才要求填写/确认 SN：优先用传入值，为空则回退到开始预填的 SN，
        仍为空则拒绝下发。
        """
        if not self._busy or self._phase != WRITE or self._pending is None:
            return False
        sn = (sn or "").strip() or self._sn
        if not sn:
            self.logmsg.emit("请先填写 SN 再确认写入", "err")
            self.status.emit("填写SN后确认写入")
            return False
        self._sn = sn
        iac, uc, pac = self._pending
        self.logmsg.emit(f"SN={sn} 确认写入: iac={iac} uc={uc} pac={pac}", "ok")
        ok = self.ble.write_cal_coeffs(iac, uc, pac)
        if not ok:
            self._retry_or_fail("下发失败")
            return False
        self._set_phase(RESULT, WATCH_RESULT)
        return True

    def _calibrate(self):
        mean = self._mean_frame()
        s = self._stds
        if (s["mv"] > 0 and mean.voltage_mv == 0) or \
           (s["ma"] > 0 and mean.current_ma == 0) or \
           (s["mw"] > 0 and mean.power_mw == 0):
            self.logmsg.emit("警告: 标准值>0但实测=0的通道将保持原系数", "err")
        details = proto.coeff_details(mean, s["mv"], s["ma"], s["mw"])
        iac, uc, pac = (d["new"] for d in details)  # noqa: C416
        self._pending = (iac, uc, pac)
        self.logmsg.emit(
            f"均值 V={mean.voltage_mv}mV I={mean.current_ma}mA P={mean.power_mw}mW | "
            f"拟写入 iac={iac} uc={uc} pac={pac} (待人工确认)", "info")
        for d in details:
            self.logmsg.emit(self._fmt_calc(d), "dbg")
        self.logmsg.emit("测量完成，请核对数值；填写 SN 后点击【确认写入系数】", "ok")
        # 不再自动下发；等待 confirm_write() 人工触发，超时由看门狗 0 关闭
        self._set_phase(WRITE, 0)
        self.status.emit("填写SN后确认写入")

    # ------------------------------------------------------------------ #
    # 完成
    # ------------------------------------------------------------------ #
    def _finish(self, result: str, note: str):
        rec = self._record_for(result, note)
        self.logmsg.emit(f"完成: {result} {note}", "ok" if result == "PASS" else "err")
        self._reset_to_idle()
        self.finished.emit(rec)

    def _record_for(self, result: str, note: str) -> dict:
        mean = self._mean_frame() if self._frames else None
        new = self._pending
        rec = {
            "time": self._now(),
            "sn": self._sn,
            "result": result,
            "note": note,
            "attempt": self._attempt,
            "std_v": self._stds["mv"] / 1000.0,
            "std_a": self._stds["ma"] / 1000.0,
            "std_p": self._stds["mw"] / 1000.0,
            "meas_v": mean.voltage_mv if mean else "",
            "meas_a": mean.current_ma if mean else "",
            "meas_p": mean.power_mw if mean else "",
            "iac0": mean.iac if mean else "",
            "uc0": mean.uc if mean else "",
            "pac0": mean.pac if mean else "",
            "iac": new[0] if new else "",
            "uc": new[1] if new else "",
            "pac": new[2] if new else "",
        }
        if self.rec is not None:
            try:
                self.rec.append(rec)
            except Exception as e:  # noqa: BLE001
                self.logmsg.emit(f"写记录失败: {e}", "err")
        return rec

    def _reset_to_idle(self):
        self._watch.stop()
        self.ble.cancel()
        self._frames.clear()
        self._pending = None
        self._to_drop = DROP_FIRST_FRAMES
        self._target_key = None
        self._busy = False
        self._phase = IDLE
        self._sn = ""
        self.status.emit(IDLE)

    @staticmethod
    def _fmt_calc(d: dict) -> str:
        """把单通道换算过程渲染成一行文本。"""
        hx = lambda x: f"0x{x:04X}"  # noqa: E731
        if d["skipped"]:
            reason = "标准=0" if d["std"] <= 0 else "实测=0"
            return (f"{d['name']}: {reason} 不校准, 保持 "
                    f"{d['old']}({hx(d['old'])})")
        note = " (超限限幅)" if round(d["raw"]) != d["new"] else ""
        return (f"{d['name']}: {d['old']}({hx(d['old'])}) × {d['std']} ÷ "
                f"{d['meas']} = {d['raw']:g} → 新 {d['new']}({hx(d['new'])}){note}")

    def _emit_live(self):
        last = self._frames[-1] if self._frames else None
        mean = new = cal = None
        if last is not None:
            win = self._frames[-min(self._avg_n, len(self._frames)):]
            mean = proto.TestFrame(
                voltage_mv=sum(f.voltage_mv for f in win) // len(win),
                current_ma=sum(f.current_ma for f in win) // len(win),
                power_mw=sum(f.power_mw for f in win) // len(win),
                iac=last.iac, uc=last.uc, pac=last.pac)
            cal = proto.coeff_details(mean, self._stds["mv"],
                                      self._stds["ma"], self._stds["mw"])
            new = tuple(d["new"] for d in cal)
        self.live.emit({
            "sn": self._sn, "phase": self._phase,
            "frame": last, "mean": mean, "new": new, "cal": cal,
            "count": len(self._frames), "need": self._avg_n,
            "std": dict(self._stds),
        })

    @staticmethod
    def _now() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
