# -*- coding: utf-8 -*-
"""HLW8112 BLE 批量校准台 入口。

运行：python main.py   (需要 Python3.10+ 与 PySide6，见 requirements.txt)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util  # noqa: E402

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from controller import Controller  # noqa: E402
from recorder import Recorder  # noqa: E402
from ui import MainWindow  # noqa: E402


def _check_bluetooth() -> str | None:
    """返回缺模块的提示；全部就绪返回 None。"""
    if importlib.util.find_spec("PySide6") is None:
        return ("未安装 PySide6。\n请执行:  pip install PySide6")
    if importlib.util.find_spec("bleak") is None:
        return ("未安装 bleak(BLE 传输层)。\n请执行:  pip install bleak")
    return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("HLW8112 BLE 批量校准台")
    missing = _check_bluetooth()
    if missing:
        QMessageBox.critical(None, "缺少蓝牙模块", missing + "\n\n"
                             "(Windows 另需开启系统蓝牙)")
        return 1

    from ble import BleClient  # noqa: PLC0415   (确保 bleak 已就绪再导入)
    ble = BleClient()
    rec = Recorder()
    ctrl = Controller(ble, rec)
    win = MainWindow(ctrl, rec, ble)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
