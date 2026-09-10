# -*- coding: utf-8 -*-
"""BLE 客户端封装：持续扫描(展示所有设备) -> 手动选择 -> 连接 -> 订阅 -> 收发帧。

传输层使用 bleak(asyncio + WinRT)，运行在独立后台线程的事件循环中，
主线程(Qt GUI)只做"提交协程 + 接收信号"，不再被 WinRT 回调阻塞(修复 UI 卡死)。

对外信号：message/status/ready/data/link_lost/devices_changed；
对外函数：start_scan/stop_scan/clear_devices/device_list/device_by_key/
connect_selected/cancel/disconnect_link/connected_name/write_cal_coeffs。
controller 与 ui 依赖的接口保持不变。
"""
from __future__ import annotations

import asyncio
import threading
import time

from PySide6.QtCore import QObject, Signal
from bleak import BleakClient, BleakScanner

import protocol as proto

DEVICE_NAME = "CALIB_DEV"    # 目标广播名
SERVICE_UUID = 0xFD50
WRITE_UUID = 0x0001
NOTIFY_UUID = 0x0002

EMIT_INTERVAL_MS = 400       # 设备列表信号节流
SCAN_SLEEP_S = 0.2           # 扫描循环检查周期
CONNECT_TIMEOUT_S = 20       # 单次连接超时
SVC_TIMEOUT_S = 15           # 订阅超时


def _looks_like_addr(s: str) -> bool:
    return ":" in s


def _char16(ch) -> str | None:
    """从特征完整 UUID 里取低 16 位句柄(如 '0002')。"""
    try:
        return ch.uuid.split("-")[0][-4:].lower()
    except (AttributeError, IndexError):
        return None


def _find_char(client, low16: int):
    """按特征低 16 位在已发现服务里找实际特征对象。

    本设备特征是厂商扩展形式(…1001-8001-00805f9b07d0)，
    bleak 用标准 host 形式 UUID(…1000-8000-00805f9b34fb)查找会
    “was not found!”，因此必须遍历并按低 16 位匹配。
    """
    want = f"{low16:04x}"
    for svc in client.services:
        for ch in svc.characteristics:
            if _char16(ch) == want:
                return ch
    return None


class BleClient(QObject):
    message = Signal(str, str)
    status = Signal(str)
    ready = Signal()
    data = Signal(bytes)
    link_lost = Signal(str)
    # 扫描状态变化
    scanning_changed = Signal(bool)
    # 设备列表快照(list[dict]: key/name/addr/rssi/target)，按 目标优先+RSSI 排序
    devices_changed = Signal(list)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._devices: dict[str, dict] = {}   # key -> {key/dev_addr/name/addr/rssi/target/device}
        self._scanning = False
        self._ready = False                   # 是否已订阅成功(可下发)
        self._connected_device = ""
        self._by_user = False                 # True: 主动断开，不触发 link_lost
        self._dirty = False
        self._last_pub = 0.0
        self._scanner: BleakScanner | None = None
        self._client: BleakClient | None = None
        self._scan_task: asyncio.Task | None = None
        self._conn_task: asyncio.Task | None = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop_runner, name="ble-worker", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # 后台事件循环
    # ------------------------------------------------------------------ #
    def _loop_runner(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _submit(self, coro):
        """把协程投递到后台循环(不阻塞主线程)。"""
        if self._loop.is_closed():
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            return None

    # ------------------------------------------------------------------ #
    # 扫描(持续，供界面选择)
    # ------------------------------------------------------------------ #
    def start_scan(self) -> bool:
        if self._scanning:
            return False
        self._scanning = True
        self.scanning_changed.emit(True)
        self._emit("开始扫描(所有 BLE 设备)...", "info")
        self.status.emit("扫描设备中 ...")
        task = self._scan_task
        if task is None or task.done():
            self._submit(self._scanner_worker())
        else:
            # 上一轮正在收尾：结束后若仍需要扫描则自动续一轮
            self._submit(self._ensure_scanner())
        return True

    def stop_scan(self):
        if not self._scanning:
            return
        self._scanning = False
        self.scanning_changed.emit(False)

    def clear_devices(self):
        """清空已发现列表并立即发布(界面表格同步清空)。"""
        self._devices.clear()
        self._dirty = False
        self.devices_changed.emit([])

    def device_list(self) -> list[dict]:
        rows = []
        for d in self._devices.values():
            rows.append({
                "key": d["key"], "name": d["name"], "addr": d["addr"],
                "rssi": d["rssi"], "target": d["target"]})
        rows.sort(key=lambda r: (not r["target"], -r["rssi"]))
        return rows

    def device_by_key(self, key: str):
        return self._devices.get(key)

    # ------------------------------------------------------------------ #
    # 后台扫描循环
    # ------------------------------------------------------------------ #
    async def _scanner_worker(self):
        self._scan_task = asyncio.current_task()
        try:
            while self._scanning:
                await self._scan_once()
                if self._scanning:
                    await asyncio.sleep(0.1)   # stop->start 竞态缓冲
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            self._emit(f"扫描异常: {e}", "err")
        finally:
            self._publish_now()
            if self._scan_task is asyncio.current_task():
                self._scan_task = None

    async def _scan_once(self):
        self._scanner = BleakScanner(self._on_detect)
        try:
            await self._scanner.start()
        except Exception as e:  # noqa: BLE001
            self._emit(f"启动扫描失败: {e}", "err")
            self._scanner = None
            if self._scanning:
                await asyncio.sleep(1.0)   # 失败后稍等再试
            return
        try:
            while self._scanning:
                await asyncio.sleep(SCAN_SLEEP_S)
                self._maybe_publish()
        finally:
            try:
                await self._scanner.stop()
            except Exception:
                pass
            self._scanner = None

    async def _ensure_scanner(self):
        try:
            await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            return
        if self._scanning and (self._scan_task is None
                               or self._scan_task.done()):
            self._submit(self._scanner_worker())

    def _on_detect(self, device, adv):
        addr = (device.address or "").strip()
        name = (device.name or adv.local_name or "").strip() or "(无名称)"
        key = addr or f"n:{name}"
        rssi = int(adv.rssi) if getattr(adv, "rssi", None) is not None else 0
        target = name == DEVICE_NAME
        old = self._devices.get(key)
        if old is None:
            # 信号无效(RSSI 0/127)的非目标设备不入列表，避免刷出无效行
            if not target and (rssi == 0 or rssi >= 127):
                return
            self._devices[key] = {
                "key": key, "dev_addr": addr, "name": name,
                "addr": addr or f"#{(len(self._devices) + 1):03d}",
                "rssi": rssi, "target": target, "device": device,
            }
            self._emit(f"发现 {name} {addr or '-'} RSSI={rssi}", "info")
        else:
            if rssi != 0 and rssi < 127:   # 无效信号不覆盖已有有效值
                old["rssi"] = rssi
            if name != "(无名称)":
                old["name"] = name
                old["target"] = old["target"] or target
        self._dirty = True

    def _maybe_publish(self):
        if self._dirty and \
                (time.monotonic() - self._last_pub) * 1000 >= EMIT_INTERVAL_MS:
            self._publish_now()

    def _publish_now(self):
        self._last_pub = time.monotonic()
        self._dirty = False
        self.devices_changed.emit(self.device_list())

    # ------------------------------------------------------------------ #
    # 连接(GATT) — controller/UI 选定后调用
    # ------------------------------------------------------------------ #
    def connect_selected(self, key: str) -> bool:
        self.stop_scan()
        d = self._devices.get(key)
        if d is None:
            self._emit(f"设备 {key} 已不在列表", "err")
            return False
        self._ready = False
        self._connected_device = f"{d['name']} {d['addr']}".strip()
        self._submit(self._do_connect(d))
        return True

    async def _do_connect(self, d: dict):
        # 同一时刻只保留一个连接任务：取消旧的(例如看门狗超时后的重试)
        prev = self._conn_task
        if prev is not None and not prev.done():
            prev.cancel()
            try:
                await prev
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._conn_task = asyncio.current_task()
        try:
            # 等扫描任务完全退出(释放适配器)
            task = self._scan_task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:  # noqa: BLE001
                    pass
            await self._close_client_async()

            addr = d.get("dev_addr") or d["key"]
            if not _looks_like_addr(addr):
                self._emit(f"设备 {d['name']} 无有效地址，无法连接", "err")
                self.status.emit("连接失败")
                self._connected_device = ""
                return
            self._emit(f"连接 {self._connected_device} ...", "info")
            self.status.emit("连接设备 ...")
            self._by_user = False
            # use_cached_services=False: 强制 UNCACHED 重新发现 GATT，避免
            # Windows 服务缓存缺特征(如本设备 0x0002 曾缓存缺失)
            client = BleakClient(
                addr, disconnected_callback=self._on_disconnected_cb,
                winrt={"use_cached_services": False})
            self._client = client
            try:
                await asyncio.wait_for(client.connect(),
                                       timeout=CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._fail_connect("连接超时")
                return
            except Exception as e:  # noqa: BLE001
                self._fail_connect(f"连接失败: {e}")
                return
            self._emit("已连接，发现服务 ...", "ok")
            self.status.emit("发现服务 ...")
            for svc in client.services:
                for ch in svc.characteristics:
                    self._emit(
                        f"  svc {svc.uuid} char {ch.uuid} "
                        f"props={sorted(ch.properties)}", "dbg")
            notify_char = _find_char(client, NOTIFY_UUID)
            if notify_char is None:
                self._ready = False
                self._emit("未发现通知特征(0x0002)，无法订阅", "err")
                self.status.emit("连接失败")
                await self._close_client_async()
                return
            try:
                await asyncio.wait_for(client.start_notify(notify_char,
                                                           self._on_notify),
                                       timeout=SVC_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                # 订阅失败：断开并提示(controller 在 CONNECT 阶段据 err 重试)
                self._ready = False
                self._emit(f"订阅通知失败: {e}", "err")
                self.status.emit("连接失败")
                await self._close_client_async()
                return
            self._ready = True
            self._emit("订阅通知成功，等待测试数据 ...", "ok")
            self.status.emit("等待测试数据 ...")
            self.ready.emit()
        finally:
            if self._conn_task is asyncio.current_task():
                self._conn_task = None

    def _fail_connect(self, text: str):
        self._ready = False
        self._connected_device = ""
        self._emit(text, "err")
        self.status.emit("连接失败")
        self._submit(self._close_client_async())

    # ------------------------------------------------------------------ #
    # 连接状态
    # ------------------------------------------------------------------ #
    def cancel(self):
        """停止扫描并断开连接(主动，不触发 link_lost)。"""
        self.stop_scan()
        self._by_user = True
        self._ready = False
        self._connected_device = ""
        self._submit(self._teardown_async())

    def disconnect_link(self):
        self._by_user = True
        self._ready = False
        self._submit(self._close_client_async())

    def connected_name(self) -> str:
        return self._connected_device

    async def _teardown_async(self):
        # 先让扫描自然退出(完成 scanner.stop())，避免强杀导致适配器残留
        task = self._scan_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._scan_task = None
        t = self._conn_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._conn_task = None
        await self._close_client_async()

    def _on_disconnected_cb(self, _client):
        # bleak 在后台循环线程回调
        if self._by_user:
            self._client = None
            return
        was_ready = self._ready
        self._ready = False
        self._client = None
        if was_ready:
            # 已进入测量/下发阶段：交 controller 走重连或 PASS 流程
            self.link_lost.emit("链路断开")
        else:
            # 连接建立前/订阅前断开：以消息形式提示(controller 在 CONNECT 重试)
            self._emit("连接被中断", "err")
            self.status.emit("连接失败")
            self._connected_device = ""

    async def _close_client_async(self):
        self._ready = False
        client = self._client
        self._client = None
        if client is not None:
            self._by_user = True
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 收发
    # ------------------------------------------------------------------ #
    def _on_notify(self, _char, value: bytearray):
        self.data.emit(bytes(value))

    def write_cal_coeffs(self, iac: int, uc: int, pac: int) -> bool:
        if not self._ready or self._client is None:
            self._emit("未就绪，无法下发系数", "err")
            return False
        payload = proto.build_cal_frame(iac, uc, pac)
        self._submit(self._do_write(payload))
        return True

    async def _do_write(self, payload: bytes):
        client = self._client
        if client is None:
            return
        try:
            char = _find_char(client, WRITE_UUID)
            if char is None:
                self._emit("未发现写特征(0x0001)", "err")
                await self._close_client_async()
                return
            props = getattr(char, "properties", ())
            response = bool(props) and "write" in props
            await client.write_gatt_char(char, payload, response=response)
        except Exception as e:  # noqa: BLE001
            # 视为链路级故障：断开经 link_lost 让 controller 重连重试
            self._emit(f"写特征失败: {e}", "err")
            await self._close_client_async()
            return
        self._emit("系数已下发", "ok")

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _emit(self, text: str, kind: str):
        self.message.emit(text, kind)
