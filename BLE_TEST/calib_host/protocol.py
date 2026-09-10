# -*- coding: utf-8 -*-
"""HLW8112 蓝牙校准协议：帧编解码与系数计算。

依据《BLE校准协议.md》：
- 所有多字节字段小端序(Little-Endian)
- 测试数据 19B(0x01) / 下发系数 7B(0x02) / 校准结果 2B(0x02)
- 新系数 = round(旧系数 x 标准值 / 实测值)，clamp 到 [1, 65535]
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

CMD_TEST = 0x01  # 设备->上位机 测试数据
CMD_CAL = 0x02   # 上位机->设备 校准系数；设备->上位机 校准结果

TEST_FRAME_LEN = 19
CAL_FRAME_LEN = 7
RESULT_FRAME_LEN = 2

RESULT_OK = 0
RESULT_FAIL = 1


@dataclass
class TestFrame:
    """一帧测试数据(实测值 + 当前系数)。"""
    voltage_mv: int   # 电压 mV
    current_ma: int   # 电流 mA
    power_mw: int     # 有功功率 mW
    iac: int          # 当前 RmsIAC 系数
    uc: int           # 当前 RmsUC 系数
    pac: int          # 当前 PowerPAC 系数


def parse_test_frame(data: bytes) -> TestFrame | None:
    """解析 4.1 节测试数据帧；非法帧返回 None。"""
    if data is None or len(data) != TEST_FRAME_LEN or data[0] != CMD_TEST:
        return None
    v, c, p = struct.unpack_from("<III", data, 1)
    iac, uc, pac = struct.unpack_from("<HHH", data, 13)
    return TestFrame(v, c, p, iac, uc, pac)


def build_cal_frame(iac: int, uc: int, pac: int) -> bytes:
    """构造 4.2 节下发校准系数帧(7B)。"""
    return bytes([CMD_CAL]) + struct.pack(
        "<HHH", clamp16(iac), clamp16(uc), clamp16(pac))


def parse_result_frame(data: bytes) -> int | None:
    """解析 4.3 节校准结果(0=成功/1=失败)；非法帧返回 None。"""
    if data is None or len(data) != RESULT_FRAME_LEN or data[0] != CMD_CAL:
        return None
    return data[1]


def clamp16(x: int) -> int:
    return max(1, min(65535, x))


def compute_new_coeffs(frame: TestFrame, std_voltage_mv: int,
                       std_current_ma: int, std_power_mw: int) -> tuple[int, int, int]:
    """按协议 §5 计算新系数。

    标准值传 0 表示该通道不校准(保持原系数)；
    实测值为 0 时无法修正，同样保持原系数(避免除零/下发 0 被设备拒绝)。
    """
    return tuple(d["new"] for d in coeff_details(
        frame, std_voltage_mv, std_current_ma, std_power_mw))  # type: ignore


def coeff_details(frame: TestFrame, std_voltage_mv: int,
                  std_current_ma: int, std_power_mw: int) -> list[dict]:
    """逐通道换算过程(供界面/日志展示计算过程值)。

    每项: name通道名 / key / old旧系数 / std标准 / meas实测 /
          skipped 是否跳过 / raw 未限幅换算值 / new 最终新系数。
    """
    spec = [
        ("电流(RmsIAC)", "iac", frame.iac, std_current_ma, frame.current_ma),
        ("电压(RmsUC)", "uc", frame.uc, std_voltage_mv, frame.voltage_mv),
        ("功率(PowerPAC)", "pac", frame.pac, std_power_mw, frame.power_mw),
    ]
    out: list[dict] = []
    for name, key, old, std, meas in spec:
        skipped = std <= 0 or meas <= 0
        if skipped:
            raw = None
            new = clamp16(old)
        else:
            raw = old * std / meas
            new = clamp16(round(raw))
        out.append({"name": name, "key": key, "old": old, "std": std,
                    "meas": meas, "skipped": skipped, "raw": raw, "new": new})
    return out
