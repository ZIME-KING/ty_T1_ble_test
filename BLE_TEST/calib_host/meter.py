# -*- coding: utf-8 -*-
"""YP 系列电参数分析仪 串口读取(MODBUS RTU 主站)。

依据《YP系列电参数Modbus通讯协议》：
- 串口 9600-8-N-1，MODBUS 从站地址 1，功能码 0x03(读寄存器)
- 寄存器 0-1 电压 / 2-3 电流 / 4-5 有功功率 … 均为 Int32，所有数据放大 1000 倍
- 寄存器 80 为 Int16 单位位：bit0 电压(0=V/1=kV) bit1 电流(0=A/1=mA)
  bit2 有功功率(0=W/1=kW)

传输层用 pyserial，跑在独立后台线程里周期轮询；主线程(Qt GUI)只接收信号，
避免串口应答超时阻塞界面。对外信号：message/reading/link_changed/ports_changed。
"""
from __future__ import annotations

import struct
import threading
import time

from PySide6.QtCore import QObject, Signal
from serial.tools import list_ports

try:
    import serial
except ImportError:  # pragma: no cover - 依赖缺失时给出明确提示
    serial = None

SLAVE_ADDR = 1
BAUDRATE = 9600
DATA_START = 0           # 数据查询区起始寄存器
DATA_COUNT = 20          # 0-19: 电压/电流/有功功率/功率因素/频率…
UNIT_REG = 80            # 单位状态位寄存器
SCALE = 1000.0           # 协议规定: 所有数据放大 1000 倍
WORD_HIGH_FIRST = True   # Int32 字序: True=高字在前(MODBUS 惯例)。实测不符时改这里

POLL_INTERVAL_S = 0.5    # 轮询周期(实时刷新标准值)
RESP_TIMEOUT_S = 0.3     # 单次应答超时
SILENCE_S = 0.01         # 帧间静默(9600 下 3.5 字符 ≈ 4ms)
MAX_MISS = 5             # 连续无应答达到此值判为掉线并停止读取


# ---------------------------------------------------------------------- #
# 协议层(纯函数, 便于离线自测)
# ---------------------------------------------------------------------- #
def crc16(data: bytes) -> int:
    """MODBUS RTU CRC16 (多项式 0xA001, 初值 0xFFFF)。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_read(start: int, count: int, slave: int = SLAVE_ADDR) -> bytes:
    """构造 0x03 读寄存器请求帧(CRC 低字节在前)。"""
    body = bytes([slave, 0x03]) + struct.pack(">HH", start, count)
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def parse_read(resp: bytes, count: int, slave: int = SLAVE_ADDR) -> list[int] | None:
    """解析 0x03 应答帧，返回 count 个 16 位寄存器值；非法/校验错返回 None。"""
    if not resp or len(resp) < 5:
        return None
    if resp[0] != slave or resp[1] != 0x03:
        return None
    n = resp[2]
    if n != count * 2 or len(resp) < 3 + n + 2:
        return None
    body = resp[:3 + n]
    c = crc16(body)
    if resp[3 + n] != (c & 0xFF) or resp[3 + n + 1] != ((c >> 8) & 0xFF):
        return None
    return list(struct.unpack(">%dH" % count, resp[3:3 + n]))


def _i32(regs: list[int], idx: int) -> int:
    """取寄存器 idx/idx+1 组成的 Int32(默认高字在前)，带符号。"""
    hi, lo = int(regs[idx]), int(regs[idx + 1])
    if not WORD_HIGH_FIRST:
        hi, lo = lo, hi
    v = (hi << 16) | lo
    return v - 0x100000000 if v & 0x80000000 else v


def decode(data_regs: list[int], unit_word: int) -> dict:
    """把寄存器原始值换算成实际物理量。

    返回:
      raw_v/raw_a/raw_w : 放大 1000 倍后的原始 Int32
      disp_v/disp_a/disp_w + unit_v/unit_a/unit_w : 仪器当前单位下的显示值
      volts/amps/watts : 归一化到 V/A/W(供标准源使用)
    """
    raw_v, raw_a, raw_w = (_i32(data_regs, 0), _i32(data_regs, 2),
                           _i32(data_regs, 4))
    kv = bool(unit_word & 0x01)      # 电压: 0=V 1=kV
    ma_u = bool(unit_word & 0x02)    # 电流: 0=A 1=mA
    kw = bool(unit_word & 0x04)      # 有功: 0=W 1=kW
    disp_v, disp_a, disp_w = raw_v / SCALE, raw_a / SCALE, raw_w / SCALE
    return {
        "raw_v": raw_v, "raw_a": raw_a, "raw_w": raw_w,
        "disp_v": disp_v, "disp_a": disp_a, "disp_w": disp_w,
        "unit_v": "kV" if kv else "V",
        "unit_a": "mA" if ma_u else "A",
        "unit_w": "kW" if kw else "W",
        "volts": disp_v * (1000.0 if kv else 1.0),
        "amps": disp_a / (1000.0 if ma_u else 1.0),
        "watts": disp_w * (1000.0 if kw else 1.0),
        "unit_word": unit_word,
        "time": time.strftime("%H:%M:%S"),
    }


def _read_exact(ser, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# ---------------------------------------------------------------------- #
# 串口客户端(后台线程轮询)
# ---------------------------------------------------------------------- #
class MeterClient(QObject):
    message = Signal(str, str)      # (文本, kind) info/ok/err
    reading = Signal(dict)          # 一次成功读数(decode 的返回值)
    link_changed = Signal(bool)     # 串口打开/关闭
    ports_changed = Signal(list)    # 可用串口列表 [{device, desc}]

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._port = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------------------- 状态 ---------------------------- #
    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> str:
        return self._port

    @staticmethod
    def available_ports() -> list[dict]:
        if serial is None:
            return []
        return [{"device": p.device, "desc": p.description or ""}
                for p in list_ports.comports()]

    def refresh_ports(self) -> list[dict]:
        ports = self.available_ports()
        self.ports_changed.emit(ports)
        return ports

    # ---------------------------- 开关 ---------------------------- #
    def open(self, port: str) -> bool:
        """打开串口并开始轮询；重复调用先关旧的。"""
        if serial is None:
            self.message.emit("未安装 pyserial, 无法使用电参数仪", "err")
            return False
        if self.is_open:
            self.close()
        if not port:
            self.message.emit("未选择串口", "err")
            return False
        self._port = port
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(port,), name="meter-worker", daemon=True)
        self._thread.start()
        return True

    def close(self):
        """停止轮询并关闭串口(线程退出时自行关闭)。"""
        t = self._thread
        if t is None:
            return
        self._stop.set()
        self._thread = None
        if t is not threading.current_thread():
            t.join(timeout=1.5)

    # ---------------------------- 工作线程 ---------------------------- #
    def _run(self, port: str):
        try:
            ser = serial.Serial(port=port, baudrate=BAUDRATE,
                                bytesize=serial.EIGHTBITS,
                                parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE,
                                timeout=RESP_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            self.message.emit(f"打开串口 {port} 失败: {e}", "err")
            self.link_changed.emit(False)
            return
        self.message.emit(
            f"串口 {port} 已打开 (9600-8-N-1, 从站 {SLAVE_ADDR})", "ok")
        self.link_changed.emit(True)
        miss = 0
        try:
            while not self._stop.is_set():
                data = self._read_block(ser, DATA_START, DATA_COUNT)
                self._stop.wait(SILENCE_S)
                unit = self._read_block(ser, UNIT_REG, 1)
                if data is None or unit is None:
                    miss += 1
                    if miss == 1:
                        self.message.emit(
                            "电参数仪无应答(检查串口/波特率/站址)", "err")
                    elif miss >= MAX_MISS:
                        self.message.emit(
                            f"连续 {miss} 次无应答, 已停止读取", "err")
                        break
                else:
                    if miss:
                        self.message.emit("电参数仪恢复应答", "ok")
                    miss = 0
                    try:
                        self.reading.emit(decode(data, unit[0]))
                    except Exception as e:  # noqa: BLE001
                        self.message.emit(f"解析读数失败: {e}", "err")
                self._stop.wait(POLL_INTERVAL_S)
        finally:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.link_changed.emit(False)
            self.message.emit(f"串口 {port} 已关闭", "info")

    def _read_block(self, ser, start: int, count: int) -> list[int] | None:
        """发一帧 0x03 请求并校验应答；失败返回 None。"""
        try:
            ser.reset_input_buffer()
            ser.write(build_read(start, count))
            ser.flush()
            resp = _read_exact(ser, 3 + count * 2 + 2)
        except Exception as e:  # noqa: BLE001
            self.message.emit(f"串口读写失败: {e}", "err")
            return None
        return parse_read(resp, count)
