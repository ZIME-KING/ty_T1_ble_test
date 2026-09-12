# -*- coding: utf-8 -*-
"""HLW8112 批量校准台 主界面。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPushButton,
    QPlainTextEdit, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from controller import CHOOSE, CONNECT, WRITE, Controller

COLORS = {"info": "#c9d1d9", "ok": "#4ade80", "err": "#ff7b72", "dbg": "#8b949e"}

# 每个功能模块的主色：决定标题色块与左侧色条, 便于一眼区分模块
ACCENT = {
    "ops": "#c05621",    # 序号与操作
    "dev": "#2b6cb0",    # 蓝牙设备
    "meter": "#0f766e",  # 电参数仪(串口)
    "std": "#b7791f",    # 标准源设定
    "live": "#2f855a",   # 实时测量
}


def _build_qss() -> str:
    base = """
QWidget#central { background:#eef1f6; }

QGroupBox {
    background:#ffffff;
    border:1px solid #d7dee8;
    border-radius:8px;
    margin-top:20px;
    padding:10px;
    font-size:13px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left:12px; top:2px;
    padding:3px 12px;
    border-radius:4px;
    color:#ffffff;
    font-weight:bold;
}

QLineEdit, QSpinBox, QDoubleSpinBox {
    background:#ffffff; border:1px solid #cbd5e0;
    border-radius:6px; padding:4px 8px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border:1px solid #3182ce; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background:#f1f4f8; color:#a0aec0; border-color:#e2e8f0;
}

QPushButton {
    background:#ffffff; border:1px solid #cbd5e0;
    border-radius:6px; padding:6px 14px;
}
QPushButton:hover { background:#f0f5fb; border-color:#90b4dd; }
QPushButton:pressed { background:#e2ecf7; }
QPushButton:disabled { background:#f1f4f8; color:#a0aec0; border-color:#e2e8f0; }
QPushButton#primary { background:#f6c445; border-color:#d9a520; font-weight:bold; }
QPushButton#primary:hover { background:#ffd666; border-color:#d9a520; }
QPushButton#primary:disabled { background:#f1f4f8; color:#a0aec0; border-color:#e2e8f0; }
QPushButton#confirm { background:#bfe3c6; border-color:#8fc79a; font-weight:bold; }
QPushButton#confirm:hover { background:#d3efd9; border-color:#8fc79a; }
QPushButton#confirm:disabled { background:#f1f4f8; color:#a0aec0; border-color:#e2e8f0; }

QTableWidget {
    background:#ffffff;
    alternate-background-color:#f6f9fc;
    gridline-color:#e4eaf2;
    border:1px solid #d7dee8;
    border-radius:6px;
    selection-background-color:#cfe3fb;
    selection-color:#1a202c;
}
QHeaderView::section {
    background:#e9eff7; color:#2d3748;
    padding:6px 8px; font-weight:bold; border:none;
    border-right:1px solid #d7dee8;
    border-bottom:1px solid #d7dee8;
}
QTableWidget QTableCornerButton::section { background:#e9eff7; border:none; }

QPlainTextEdit#log {
    background:#1e222a; color:#d6dbe4;
    border:1px solid #2d333d; border-radius:8px; padding:8px;
    font-family:Consolas,monospace; font-size:12px;
}
"""
    parts = [base]
    for key, color in ACCENT.items():
        parts.append(
            f"QGroupBox#{key}Box {{ border-left:4px solid {color}; }}\n"
            f"QGroupBox#{key}Box::title {{ background:{color}; }}\n")
    return "".join(parts)


_QSS = _build_qss()


class MainWindow(QMainWindow):
    def __init__(self, controller, recorder, ble=None, meter=None):
        super().__init__()
        self.ctrl = controller
        self.rec = recorder
        self.ble = ble
        self.meter = meter
        self._last_meter: dict | None = None   # 最近一次电参数仪读数
        self.setWindowTitle("HLW8112 BLE 批量校准台")
        self.resize(1440, 800)
        self.setMinimumSize(1180, 660)

        cw = QWidget()
        cw.setObjectName("central")
        self.setCentralWidget(cw)
        self.setStyleSheet(_QSS)
        root = QVBoxLayout(cw)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # 左右双栏：左=操作与输入, 右=测量与结果
        cols = QHBoxLayout()
        cols.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        right = QVBoxLayout()
        right.setSpacing(12)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)

        # ---- 状态大字 ----
        self.lbl_banner = QLabel("待机")
        self.lbl_banner.setStyleSheet(
            "font-size:26px;font-weight:bold;color:#1a365d;letter-spacing:1px;"
            "background:#e7f0fb;border:1px solid #a9c8e8;"
            "border-left:6px solid #2b6cb0;border-radius:8px;padding:10px;")
        self.lbl_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_banner.setMinimumHeight(58)
        root.addWidget(self.lbl_banner)

        # ---- ① SN + 操作 ----
        gops = QGroupBox("① 序号与操作")
        gops.setObjectName("opsBox")
        sn_row = QHBoxLayout(gops)
        sn_row.setContentsMargins(10, 6, 10, 10)
        sn_row.setSpacing(8)
        sn_row.addWidget(QLabel("SN:"))
        self.sn_edit = QLineEdit()
        self.sn_edit.setPlaceholderText("扫描/连接后测量完成时再输入(确认写入系数时必填)")
        self.sn_edit.setMinimumHeight(30)
        self.sn_edit.returnPressed.connect(self._on_sn_enter)
        self.sn_edit.textChanged.connect(lambda *a: self._sync_dev_btns())
        sn_row.addWidget(self.sn_edit, 1)
        self.btn_start = QPushButton("开始扫描")
        self.btn_start.clicked.connect(self._on_sn_enter)
        self.btn_write = QPushButton("确认写入系数")
        self.btn_write.setObjectName("primary")
        self.btn_write.setEnabled(False)
        self.btn_write.clicked.connect(self._confirm_write)
        self.btn_abort = QPushButton("中止当前")
        self.btn_abort.setEnabled(False)
        self.btn_abort.clicked.connect(self.ctrl.abort)
        sn_row.addWidget(self.btn_start)
        sn_row.addWidget(self.btn_write)
        sn_row.addWidget(self.btn_abort)
        left.addWidget(gops)

        # ---- ② 扫描到的蓝牙(全部列出, 人工点选确认) ----
        gdev = QGroupBox("② 扫描到的蓝牙设备 (自动刷新, 选中后点【确认连接】)")
        gdev.setObjectName("devBox")
        dv = QVBoxLayout(gdev)
        dv.setContentsMargins(10, 6, 10, 10)
        dv.setSpacing(8)
        self.dev_table = QTableWidget(0, 4)
        self.dev_table.setHorizontalHeaderLabels(["名称", "地址", "RSSI", "类型"])
        self.dev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dev_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.dev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dev_table.setAlternatingRowColors(True)
        self.dev_table.verticalHeader().setVisible(False)
        self.dev_table.setMinimumHeight(120)
        self.dev_table.itemSelectionChanged.connect(self._sync_dev_btns)
        self.dev_table.cellDoubleClicked.connect(lambda *a: self._confirm_device())
        hh = self.dev_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        dv.addWidget(self.dev_table, 1)
        dr = QHBoxLayout()
        self.lbl_dev_status = QLabel("")
        self.lbl_dev_status.setStyleSheet(
            "color:#4a5568;background:#eef1f6;border-radius:4px;padding:3px 8px;")
        dr.addWidget(self.lbl_dev_status, 1)
        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.clicked.connect(self._on_rescan)
        self.btn_confirm = QPushButton("确认连接所选设备")
        self.btn_confirm.setObjectName("confirm")
        self.btn_confirm.setEnabled(False)
        self.btn_confirm.clicked.connect(self._confirm_device)
        dr.addWidget(self.btn_rescan)
        dr.addWidget(self.btn_confirm)
        dv.addLayout(dr)
        left.addWidget(gdev, 1)
        self._dev_rows: dict[int, str] = {}   # 行号 -> 设备 key
        if ble is not None:
            ble.devices_changed.connect(self._on_devices)
            ble.scanning_changed.connect(self._on_scanning)

        # ---- ③ 电参数仪(串口 MODBUS RTU, 可作实时标准源) ----
        gmt = QGroupBox("③ 电参数仪 (Modbus RTU 9600-8-N-1, 从站 1)")
        gmt.setObjectName("meterBox")
        g3 = QGridLayout(gmt)
        g3.setContentsMargins(10, 6, 10, 10)
        g3.setHorizontalSpacing(8)
        g3.setVerticalSpacing(8)
        g3.setColumnStretch(1, 1)
        g3.addWidget(QLabel("串口"), 0, 0)
        self.cb_port = QComboBox()
        g3.addWidget(self.cb_port, 0, 1)
        self.btn_mrefresh = QPushButton("刷新")
        self.btn_mrefresh.clicked.connect(self._on_port_refresh)
        g3.addWidget(self.btn_mrefresh, 0, 2)
        self.btn_mopen = QPushButton("连接电参数仪")
        self.btn_mopen.setObjectName("confirm")
        self.btn_mopen.clicked.connect(self._on_meter_toggle)
        g3.addWidget(self.btn_mopen, 0, 3)
        self.lbl_meter = QLabel("未连接")
        self.lbl_meter.setStyleSheet(
            "font-family:Consolas;font-size:12px;color:#22543d;"
            "background:#f0fbf9;border:1px solid #b2e0d8;"
            "border-radius:4px;padding:5px 8px;")
        g3.addWidget(self.lbl_meter, 1, 0, 1, 4)
        self.chk_mstd = QCheckBox("取电参数仪实时值为标准源(勾选后 ④ 自动跟随)")
        self.chk_mstd.setEnabled(False)
        self.chk_mstd.toggled.connect(self._on_std_src_toggled)
        g3.addWidget(self.chk_mstd, 2, 0, 1, 4)
        left.addWidget(gmt)

        if meter is not None:
            meter.message.connect(self._on_log)
            meter.reading.connect(self._on_meter_reading)
            meter.link_changed.connect(self._on_meter_link)
            meter.ports_changed.connect(self._on_ports)
            self._on_ports(meter.available_ports())
        else:
            self.lbl_meter.setText(
                "未安装 pyserial, 电参数仪不可用 (pip install pyserial)")
            for w in (self.cb_port, self.btn_mrefresh, self.btn_mopen):
                w.setEnabled(False)

        # ---- ④ 标准源参数 ----
        gb = QGroupBox("④ 标准源设定 (填 0 表示该通道不校准; 勾选③后自动跟随仪器)")
        gb.setObjectName("stdBox")
        gp = QGridLayout(gb)
        gp.setContentsMargins(10, 6, 10, 10)
        gp.setHorizontalSpacing(10)
        gp.setVerticalSpacing(8)
        gp.setColumnStretch(1, 1)
        gp.setColumnStretch(3, 1)
        self.sb_v = self._spin(0, 1000, 3, " V")
        self.sb_a = self._spin(0, 100, 4, " A")
        self.sb_w = self._spin(0, 100000, 3, " W")
        self.sb_v.setValue(220.0)
        self.sb_a.setValue(0.36)
        self.sb_w.setValue(79.2)
        gp.addWidget(QLabel("标准电压"), 0, 0)
        gp.addWidget(self.sb_v, 0, 1)
        gp.addWidget(QLabel("标准电流"), 0, 2)
        gp.addWidget(self.sb_a, 0, 3)
        gp.addWidget(QLabel("标准功率"), 1, 0)
        gp.addWidget(self.sb_w, 1, 1)
        gp.addWidget(QLabel("平均帧数"), 1, 2)
        self.sb_avg = QSpinBox()
        self.sb_avg.setRange(3, 30)
        self.sb_avg.setValue(8)
        self.sb_avg.setSuffix(" 帧(约1s/帧)")
        gp.addWidget(self.sb_avg, 1, 3)
        self.lbl_recpath = QLabel("")
        self.lbl_recpath.setStyleSheet("color:#4a5568;")
        gp.addWidget(self.lbl_recpath, 2, 0, 1, 3)
        btn_open = QPushButton("打开记录目录")
        btn_open.clicked.connect(self._open_log_dir)
        gp.addWidget(btn_open, 2, 3)
        left.addWidget(gb)

        # ---- ⑤ 实测实时面板 ----
        gb2 = QGroupBox("⑤ 实时测量")
        gb2.setObjectName("liveBox")
        g2 = QGridLayout(gb2)
        g2.setContentsMargins(10, 6, 10, 10)
        g2.setHorizontalSpacing(14)
        g2.setVerticalSpacing(7)
        heads = ["通道", "标准值", "实测均值", "误差", "帧数"]
        self.lbl_meas = {}
        for i, h in enumerate(heads):
            th = QLabel(h)
            th.setStyleSheet("color:#2f855a;font-weight:bold;")
            g2.addWidget(th, 0, i)
        rows = [("电压", "V"), ("电流", "A"), ("功率", "W")]
        for r, (name, unit) in enumerate(rows, start=1):
            nl = QLabel(name)
            nl.setStyleSheet("color:#4a5568;")
            g2.addWidget(nl, r, 0)
            for c in range(1, 4):
                lab = QLabel("-")
                lab.setAlignment(Qt.AlignmentFlag.AlignRight)
                g2.addWidget(lab, r, c)
                self.lbl_meas[(name, c)] = lab
            self.lbl_meas[(name, 4)] = QLabel("-")
            self.lbl_meas[(name, 4)].setAlignment(Qt.AlignmentFlag.AlignRight)
            g2.addWidget(self.lbl_meas[(name, 4)], r, 4)
        g2.addWidget(QLabel("系数"), 4, 0)
        self.lbl_coeff = QLabel("iac: -    uc: -    pac: -")
        self.lbl_coeff.setStyleSheet(
            "font-family:Consolas;font-size:13px;color:#22543d;"
            "background:#f0fff4;border:1px solid #c6f6d5;"
            "border-radius:4px;padding:6px 8px;")
        g2.addWidget(self.lbl_coeff, 4, 1, 1, 4)
        g2.addWidget(QLabel("换算过程"), 5, 0)
        self.lbl_calc = QLabel("等待测量数据 ...")
        self.lbl_calc.setStyleSheet(
            "font-family:Consolas;font-size:11px;color:#2d3748;"
            "background:#f8fafc;border:1px dashed #cbd5e0;"
            "border-radius:4px;padding:6px 8px;")
        self.lbl_calc.setWordWrap(True)
        self.lbl_calc.setAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignTop)
        g2.addWidget(self.lbl_calc, 5, 1, 1, 4)
        right.addWidget(gb2)

        # ---- ⑥ 统计 + 记录表 ----
        lbl_rec = QLabel("⑥ 批量记录")
        lbl_rec.setStyleSheet(
            "font-size:15px;font-weight:bold;color:#4c3d99;"
            "background:#ede9fe;border-left:4px solid #6b46c1;"
            "border-radius:4px;padding:5px 10px;")
        right.addWidget(lbl_rec)
        self.lbl_stats = QLabel("总数 0    通过 0    失败 0")
        self.lbl_stats.setStyleSheet(
            "font-size:14px;font-weight:bold;color:#4a5568;"
            "background:#ffffff;border:1px solid #e2e8f0;"
            "border-radius:6px;padding:6px 10px;")
        right.addWidget(self.lbl_stats)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["时间", "SN", "结果", "标准V/A/W", "实测V/A/W", "新系数iac/uc/pac", "尝试", "备注"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        right.addWidget(self.table, 2)

        # ---- ⑦ 日志 ----
        lbl_log = QLabel("⑦ 日志")
        lbl_log.setStyleSheet(
            "font-size:15px;font-weight:bold;color:#2d3748;"
            "background:#e2e8f0;border-left:4px solid #4a5568;"
            "border-radius:4px;padding:5px 10px;")
        right.addWidget(lbl_log)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(140)
        right.addWidget(self.log, 1)

        root.addLayout(cols, 1)

        # 信号
        controller.status.connect(self._on_status)
        controller.logmsg.connect(self._on_log)
        controller.live.connect(self._on_live)
        controller.finished.connect(self._on_finished)
        self._count = {"total": 0, "pass": 0, "fail": 0}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _spin(lo: float, hi: float, dec: int, suffix: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSingleStep(0.001 if lo == 0 else 0.01)
        s.setSuffix(suffix)
        return s

    def _on_sn_enter(self):
        """SN 框回车 / 点【开始扫描】。

        - 空闲时：直接开始扫描，不要求 SN(SN 留到确认写入时输入)
        - 确认写入阶段：回车视为确认写入
        """
        if self.ctrl.busy:
            if self.ctrl.phase == WRITE:
                self.ctrl.confirm_write(self.sn_edit.text())
            return
        ok = self.ctrl.start_unit(
            "",   # SN 不预填，避免误用上一台的号
            self.sb_v.value(), self.sb_a.value(), self.sb_w.value(),
            self.sb_avg.value())
        if ok:
            self.sn_edit.clear()
            self._set_busy_ui(True)
            self.dev_table.setRowCount(0)   # 旧设备行作废, 等新扫描列表
            self._dev_rows.clear()

    def _confirm_write(self):
        if self.ctrl.phase != WRITE:
            return
        self.ctrl.confirm_write(self.sn_edit.text())

    def _set_busy_ui(self, busy: bool):
        self.btn_abort.setEnabled(busy)
        self.btn_start.setEnabled(not busy)
        self._sync_dev_btns()

    # ------------------------------------------------------------------ #
    # 设备列表(ble.devices_changed / scanning_changed)
    # ------------------------------------------------------------------ #
    def _on_devices(self, devs: list):
        keep_key = self._selected_key()
        self.dev_table.setRowCount(0)
        self._dev_rows.clear()
        for r, d in enumerate(devs):
            self.dev_table.insertRow(r)
            self._dev_rows[r] = d["key"]
            name = QTableWidgetItem(d["name"])
            name.setData(Qt.ItemDataRole.UserRole, d["key"])
            if d["target"]:
                name.setForeground(Qt.GlobalColor.darkGreen)
            self.dev_table.setItem(r, 0, name)
            addr = QTableWidgetItem(d["addr"])
            addr.setForeground(Qt.GlobalColor.darkGray)
            self.dev_table.setItem(r, 1, addr)
            rssi = d["rssi"]
            self.dev_table.setItem(r, 2, QTableWidgetItem(
                "-" if rssi == 0 or rssi >= 127 else str(rssi)))
            typ = QTableWidgetItem("目标(设备名匹配)" if d["target"] else "其他")
            if d["target"]:
                typ.setForeground(Qt.GlobalColor.darkGreen)
            self.dev_table.setItem(r, 3, typ)
        if keep_key is not None:
            for r, k in self._dev_rows.items():
                if k == keep_key:
                    self.dev_table.selectRow(r)
                    break
        n = len(devs)
        self.lbl_dev_status.setText(f"共 {n} 台")
        self._sync_dev_btns()

    def _on_scanning(self, scanning: bool):
        self.lbl_dev_status.setText(
            "正在扫描(每8秒刷新一轮)..." if scanning else
            f"已停止扫描, 共 {self.dev_table.rowCount()} 台")
        self._sync_dev_btns()

    def _selected_key(self) -> str | None:
        sel = self.dev_table.selectedItems()
        if not sel:
            return None
        it = self.dev_table.item(sel[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _sync_dev_btns(self):
        busy, ph = self.ctrl.busy, self.ctrl.phase
        choosing = busy and ph in (CHOOSE, CONNECT)
        self.btn_confirm.setEnabled(choosing and self._selected_key() is not None)
        self.btn_rescan.setEnabled(busy and ph == CHOOSE)
        # 测量完成待确认：SN 填写完整才允许确认写入
        waiting = busy and ph == WRITE
        has_sn = bool(self.sn_edit.text().strip())
        self.btn_write.setEnabled(waiting and has_sn)
        # SN 只在空闲(准备下一台)与"确认写入"阶段可编辑
        self.sn_edit.setEnabled(not busy or waiting)
        if waiting and not has_sn:
            self.sn_edit.setFocus()
        # 采集/下发期间禁止误点, 列表仅作查看
        self.dev_table.setEnabled(not (busy and ph not in (CHOOSE, CONNECT)))

    def _confirm_device(self):
        key = self._selected_key()
        if key is None:
            return
        if not self.ctrl.select_device(key):
            self.lbl_dev_status.setText("连接未开始(设备已失效), 请重新扫描")
            self._sync_dev_btns()

    def _on_rescan(self):
        self.ctrl.rescan()

    # ------------------------------------------------------------------ #
    # 电参数仪(串口) meter.reading / link_changed / ports_changed
    # ------------------------------------------------------------------ #
    def _on_ports(self, ports: list):
        keep = self.cb_port.currentData()
        self.cb_port.clear()
        for p in ports:
            self.cb_port.addItem(f"{p['device']}  {p['desc']}".strip(), p["device"])
        if not ports:
            self.lbl_meter.setText("未发现串口, 插好 USB 转串口后点【刷新】")
            return
        if keep:
            i = self.cb_port.findData(keep)
            if i >= 0:
                self.cb_port.setCurrentIndex(i)

    def _on_port_refresh(self):
        if self.meter is not None:
            self.meter.refresh_ports()

    def _on_meter_toggle(self):
        if self.meter is None:
            return
        if self.meter.is_open:
            self.meter.close()
            return
        port = self.cb_port.currentData()
        if not port:
            self.lbl_meter.setText("未发现串口, 请插入 USB 后点【刷新】")
            return
        self.meter.open(port)

    def _on_meter_link(self, opened: bool):
        self.btn_mopen.setText("断开电参数仪" if opened else "连接电参数仪")
        self.cb_port.setEnabled(not opened)
        self.btn_mrefresh.setEnabled(not opened and self.meter is not None)
        if opened:
            return
        self.chk_mstd.setEnabled(False)
        self.chk_mstd.setChecked(False)      # 触发 _on_std_src_toggled 恢复手工输入
        self.lbl_meter.setText("未连接")

    def _on_meter_reading(self, d: dict):
        self._last_meter = d
        self.chk_mstd.setEnabled(True)
        self.lbl_meter.setText(
            f"{d['time']}  V {d['disp_v']:.4g} {d['unit_v']}"
            f"  A {d['disp_a']:.4g} {d['unit_a']}"
            f"  W {d['disp_w']:.4g} {d['unit_w']}"
            f"  (单位位 0x{d['unit_word']:02X})")
        if self.chk_mstd.isChecked():
            self._apply_meter_std(d)

    def _on_std_src_toggled(self, checked: bool):
        """勾选后 ④ 的标准值只读并实时跟随仪器读数。"""
        for sb in (self.sb_v, self.sb_a, self.sb_w):
            sb.setReadOnly(checked)
        if checked and self._last_meter is not None:
            self._apply_meter_std(self._last_meter)

    def _apply_meter_std(self, d: dict):
        for sb, val in ((self.sb_v, d["volts"]), (self.sb_a, d["amps"]),
                        (self.sb_w, d["watts"])):
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)
        self.ctrl.set_stds(d["volts"], d["amps"], d["watts"])

    # ------------------------------------------------------------------ #
    def _on_status(self, text: str):
        self.lbl_banner.setText(text)
        self._sync_dev_btns()

    def _on_log(self, text: str, kind: str):
        color = COLORS.get(kind, "#666")
        self.log.appendHtml(
            f'<span style="color:{color}">{text.replace(" ", "&nbsp;")}</span>')

    def _on_live(self, d: dict):
        fr, mean, new = d["frame"], d["mean"], d["new"]
        self.lbl_banner.setText(f"{d['phase']}   SN={d['sn']}   帧 {d['count']}/{d['need']}")
        if mean is not None:
            data = [("电压", mean.voltage_mv / 1000.0, d["std"]["mv"] / 1000.0),
                    ("电流", mean.current_ma / 1000.0, d["std"]["ma"] / 1000.0),
                    ("功率", mean.power_mw / 1000.0, d["std"]["mw"] / 1000.0)]
            for (name, c) in [("电压", 1), ("电流", 2), ("功率", 3)]:
                meas, std = data[c - 1][1], data[c - 1][2]
                self.lbl_meas[(name, 1)].setText(f"{std:.4g}")
                self.lbl_meas[(name, 2)].setText(f"{meas:.4g}")
                if std > 0 and meas != 0:
                    self.lbl_meas[(name, 3)].setText(f"{(meas - std) / std * 100:+.2f}%")
                else:
                    self.lbl_meas[(name, 3)].setText("-")
            self.lbl_meas[("电压", 4)].setText(str(d["count"]))
            if new:
                self.lbl_coeff.setText(
                    f"旧 iac=0x{fr.iac:04X}({fr.iac})  uc=0x{fr.uc:04X}({fr.uc})  pac=0x{fr.pac:04X}({fr.pac})  |  "
                    f"新 iac={new[0]}  uc={new[1]}  pac={new[2]}")
            if d.get("cal"):
                self.lbl_calc.setText(
                    "\n".join(Controller._fmt_calc(c) for c in d["cal"]))
        elif fr is not None:
            self.lbl_meas[("电压", 4)].setText(str(d["count"]))

    def _on_finished(self, rec: dict):
        self._count["total"] += 1
        if rec["result"] == "PASS":
            self._count["pass"] += 1
        else:
            self._count["fail"] += 1
        st = self._count
        self.lbl_stats.setText(
            f"总数 {st['total']}    通过 {st['pass']}    失败 {st['fail']}")
        row = self.table.rowCount()
        self.table.insertRow(row)
        vals = [
            rec["time"], rec["sn"], rec["result"],
            f"{rec['std_v']:g}/{rec['std_a']:g}/{rec['std_p']:g}",
            f"{rec['meas_v']}/{rec['meas_a']}/{rec['meas_p']}",
            f"{rec['iac']}/{rec['uc']}/{rec['pac']}",
            f"{rec['attempt']}", rec["note"],
        ]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            if rec["result"] == "PASS":
                it.setForeground(Qt.GlobalColor.darkGreen)
            elif rec["result"] == "FAIL":
                it.setForeground(Qt.GlobalColor.red)
            self.table.setItem(row, c, it)
        self.table.scrollToBottom()
        self._set_busy_ui(False)
        self.sn_edit.setFocus()
        self._update_recpath()

    def _update_recpath(self):
        p = self.rec.current_path()
        self.lbl_recpath.setText(f"记录: {p}" if p else "记录: 首个完成后生成")

    def _open_log_dir(self):
        p = self.rec.current_path()
        target = str(p.parent if p else self.rec.log_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def closeEvent(self, ev):
        if self.meter is not None:
            self.meter.close()
        self.ctrl.shutdown()
        super().closeEvent(ev)
