# ty_T1_ble_test

HLW8112 BLE 批量校准台（上位机）。

## 运行环境

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11，需开启系统蓝牙 |
| Python | 3.10 及以上（实测 3.12） |

## 依赖安装

```
pip install -r BLE_TEST/calib_host/requirements.txt
```

- `PySide6-Essentials` 已包含 QtCore / QtGui / QtWidgets，界面所需模块齐全。无需安装完整的 `PySide6`——完整版会额外带上 WebEngine、Multimedia 等约 300MB 的附加模块，本项目并未使用。
- BLE 传输层使用 `bleak`（asyncio + WinRT），不依赖 QtBluetooth。

## 启动

```
python BLE_TEST/calib_host/main.py
```

冒烟测试（不连实机，走内置模拟）：

```
python BLE_TEST/calib_host/run_smoke.py
```

## 运行时目录（不入库）

运行时与工具链体积较大，不纳入版本控制（规则见 `.gitignore`），统一放在**仓库目录之外**，本机约定位置与仓库同级：

```
BLE_TEST/
├─ runtime/                      ← 运行时与安装包，不入库
│  ├─ .dotnet/                   ← .NET SDK（可用 BLE_TEST/dotnet-install.ps1 重新获取）
│  ├─ .pyruntime/                ← Python 3.12 embeddable + PySide6
│  │  └─ py/python.exe
│  └─ python-3.12.10-amd64.exe   ← Python 安装包
└─ ty_T1_ble_test/               ← 本仓库
```

源码不引用上述路径，因此运行库位置可以随意调整。若想直接使用外置运行时（无需本机全局安装 Python），在仓库根目录执行：

```
..\runtime\.pyruntime\py\python.exe BLE_TEST\calib_host\main.py
```

> 注意：`.gitignore` 挡的是「不被跟踪」，文件仍在磁盘上。新增运行库时请直接放到 `runtime/`，不要放进仓库目录。

## 目录结构

```
BLE_TEST/
├─ calib_host/         校准台上位机源码
│  ├─ main.py          程序入口
│  ├─ ui.py            界面
│  ├─ controller.py    业务状态机
│  ├─ ble.py           BLE 传输层（bleak，独立线程事件循环）
│  ├─ protocol.py      协议编解码
│  ├─ recorder.py      数据记录
│  └─ run_smoke.py     冒烟测试
├─ calib_logs/         校准记录（CSV）
├─ e2e_probe.py        端到端探针
├─ dotnet-install.ps1  .NET SDK 获取脚本
└─ BLE校准协议.md       协议说明
```
