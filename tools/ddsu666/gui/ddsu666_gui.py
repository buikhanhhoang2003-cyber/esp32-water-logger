#!/usr/bin/env python3
"""DDSU666 tool: read and configure a CHINT DDSU666 meter over RS485.

The meter is reached through an ESP32 running ../firmware (UART2, TX16/RX17),
through a USB-RS485 adapter, or through a built-in simulator.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from ddsu666 import __version__
from ddsu666 import registers as R
from ddsu666.client import DDSU666Client, Reading, ScanHit, combined_energy
from ddsu666.modbus import EXCEPTION_TEXT, ModbusError, crc_ok, parse_int, words_to_float
from ddsu666.simulator import SimulatedDDSU666
from ddsu666.transport import (BridgeTransport, DirectTransport, LinkLost, SimTransport, char_time_s,
                               list_serial_ports)

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".ddsu666_tool.json")
DEFAULTS = {
    "mode": "bridge", "port": "", "baud": 9600, "fmt": "8N1", "slave": 1,
    "timeout_ms": 300, "retries": 2, "word_order": "ABCD", "poll_ms": 1000,
}
MODES = (
    ("bridge", "ESP32 cầu nối RS485 (firmware rs485_bridge)"),
    ("direct", "USB-RS485 trực tiếp"),
    ("sim", "Mô phỏng (không cần phần cứng)"),
)
MODE_BY_LABEL = {label: key for key, label in MODES}
LABEL_BY_MODE = dict(MODES)

# Big tiles on the measurement tab: register address (or "ComEp"), title, unit, decimals.
TILES = (
    (R.VOLTAGE.address, "Điện áp U", "V", 2),
    (R.CURRENT.address, "Dòng điện I", "A", 3),
    (R.FREQUENCY.address, "Tần số F", "Hz", 2),
    (R.ACTIVE_POWER.address, "Công suất tác dụng P", "kW", 4),
    (R.REACTIVE_POWER.address, "Công suất phản kháng Q", "kvar", 4),
    (R.POWER_FACTOR.address, "Hệ số công suất PF", "", 3),
    (R.IMPORT_ENERGY.address, "Điện năng thuận Ep (Imp)", "kWh", 2),
    (R.EXPORT_ENERGY.address, "Điện năng ngược −Ep (Exp)", "kWh", 2),
    ("ComEp", "Điện năng tổng hợp ComEp", "kWh", 2),
)
DECIMALS = {address: digits for address, _, _, digits in TILES}
CSV_COLUMNS = (
    ("U (V)", R.VOLTAGE.address), ("I (A)", R.CURRENT.address), ("P (kW)", R.ACTIVE_POWER.address),
    ("Q (kvar)", R.REACTIVE_POWER.address), ("PF", R.POWER_FACTOR.address), ("F (Hz)", R.FREQUENCY.address),
    ("Ep thuận (kWh)", R.IMPORT_ENERGY.address), ("Ep ngược (kWh)", R.EXPORT_ENERGY.address),
)
LOG_LABELS = {"tx": "TX", "rx": "RX", "info": "··", "warn": "!!", "err": "LỖI", "dev": "ESP"}
NOTES = (
    "• Đồng hồ chỉ hỗ trợ lệnh 03H (đọc) và 10H (ghi nhiều thanh ghi) — app ghi mọi thông số bằng 10H.\n"
    "• Định dạng khung (8N1/8N2/8E1/8O1) và giao thức chỉ đổi được bằng nút trên đồng hồ: nhấn giữ, "
    "nhấn nhanh để chuyển 8n2 → 8n1 → 8E1 → 8o1 → 645 (Hình 3); địa chỉ qua nút chỉ đặt được 1–99.\n"
    "• Đổi Addr hoặc BAud: app tự chuyển sang địa chỉ/tốc độ mới rồi đọc lại để xác nhận.\n"
    "• ChangeProtocol = 1 (DL/T 645-2007) làm đồng hồ ngừng trả lời Modbus; muốn quay lại phải dùng nút.\n"
    "• ClrE = 1 xóa điện năng tích lũy về 0 — không hoàn tác được.\n"
    "• Tài liệu không nói có cần ghi UCode (mật khẩu) trước khi đổi thông số. Nếu lệnh ghi bị từ chối, "
    "ghi lại đúng mật khẩu hiện tại vào UCode rồi thử lại (ghi lại giá trị cũ không làm đổi mật khẩu).\n"
    "• Giá trị đo là phía thứ cấp: với bản DDSU666-CT chưa nhân tỉ số biến dòng."
)


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            stored = json.load(handle)
        settings.update({key: stored[key] for key in DEFAULTS if key in stored})
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if settings["baud"] not in R.LINE_BAUDS:
        settings["baud"] = DEFAULTS["baud"]
    return settings


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
    except OSError:
        pass


def fmt_number(value, digits: int) -> str:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}f}"


def fmt_word(value: int) -> str:
    return f"{value}  ({value & 0xFFFF:04X}H)"


def describe_answer(request: bytes, answer: bytes) -> str:
    """Human-readable reading of a raw reply on the raw-frame panel."""
    if not answer:
        return "Không có phản hồi."
    text = f"Nhận {len(answer)} byte: {answer.hex(' ').upper()}\n"
    if not crc_ok(answer):
        return text + "CRC sai hoặc khung không trọn vẹn."
    if answer[1] & 0x80:
        code = answer[2] if len(answer) > 2 else 0
        return text + f"Đồng hồ trả mã lỗi {code:02X}H: {EXCEPTION_TEXT.get(code, 'không rõ')}."
    if answer[1] == 0x03 and len(answer) >= 5:
        data = answer[3:-2]
        words = struct.unpack(f">{len(data) // 2}H", data[: len(data) // 2 * 2])
        return text + "CRC đúng. Dữ liệu: " + " ".join(f"{word:04X}" for word in words)
    if answer[1] == 0x10 and len(answer) == 8:
        address, count = struct.unpack(">HH", answer[2:6])
        return text + f"CRC đúng. Đã ghi {count} thanh ghi từ {address:04X}H."
    return text + "CRC đúng."


class Worker:
    """Runs every meter operation on one background thread, in submission order."""

    def __init__(self, post):
        self._jobs: queue.Queue = queue.Queue()
        self._post = post
        self._thread = threading.Thread(target=self._run, name="ddsu666-io", daemon=True)
        self._thread.start()

    def submit(self, fn, on_ok=None, on_err=None) -> None:
        self._jobs.put((fn, on_ok, on_err))

    def stop(self, timeout: float = 3.0) -> None:
        self._jobs.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, on_ok, on_err = job
            try:
                result = fn()
            except Exception as exc:  # reported on the UI thread
                self._post(on_err, exc)
            else:
                self._post(on_ok, result)


class Tile:
    def __init__(self, parent, title: str, unit: str, digits: int):
        self.frame = ttk.LabelFrame(parent, text=title, padding=(10, 0, 10, 4))
        self.digits = digits
        self.value_var = tk.StringVar(value="—")
        self.extra_var = tk.StringVar(value="")
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.value_var, style="Value.TLabel", anchor="e").pack(
            side="left", fill="x", expand=True)
        ttk.Label(row, text=unit, style="Unit.TLabel", width=5).pack(side="left", padx=(6, 0))
        ttk.Label(self.frame, textvariable=self.extra_var, style="Hint.TLabel", anchor="e").pack(fill="x")

    def set(self, value, extra: str = "") -> None:
        self.value_var.set(fmt_number(value, self.digits))
        self.extra_var.set(extra)


class App:
    def __init__(self, root: tk.Tk, overrides: dict | None = None, autoconnect: bool = False):
        self.root = root
        self.settings = load_settings()
        self.settings.update(overrides or {})
        self.events: queue.Queue = queue.Queue()
        self.worker = Worker(self._post)
        self.transport = None
        self.client: DDSU666Client | None = None
        self.connected = False
        self.connecting = False
        self.closing = False
        self.poll_inflight = False
        self.poll_started = 0.0
        self.poll_errors = 0
        self.scanning = False
        self.scan_cancel = threading.Event()
        self.scan_total = 1
        self.csv_file = None
        self.csv_writer = None
        self.sim_meter: SimulatedDDSU666 | None = None
        self.needs_connection: list = []
        self.line_widgets: list = []

        self._init_style()
        self._build()
        self._refresh_ports()
        if overrides and overrides.get("port") and self._selected_port() != overrides["port"]:
            self.port_var.set(overrides["port"])  # not enumerated (yet): keep what was asked for
        self._mode_changed()
        self._set_connected(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(40, self._drain_events)
        if autoconnect:
            self.root.after(300, self._toggle_connect)

    # ------------------------------------------------------------------ plumbing

    def _post(self, callback, argument) -> None:
        """Called from the worker thread: hand a result to the UI thread."""
        if callback is not None:
            self.events.put(("call", callback, argument))

    def _log_any_thread(self, kind: str, text: str) -> None:
        self.events.put(("log", kind, text, time.time()))

    def _drain_events(self) -> None:
        if self.closing:
            return
        for _ in range(400):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item[0] == "log":
                self._append_log(*item[1:])
                continue
            try:
                item[1](item[2])
            except Exception as exc:  # a bug in a callback must not stop the event pump
                self._append_log("err", f"Lỗi nội bộ GUI: {type(exc).__name__}: {exc}", time.time())
        self.root.after(40, self._drain_events)

    def run(self, fn, on_ok=None, on_err=None, status: str | None = None) -> None:
        if status:
            self._status(status)
        self.worker.submit(fn, on_ok, on_err or self._job_failed)

    def _job_failed(self, exc: Exception, what: str = "") -> None:
        if not self.connected and not self.connecting:
            return  # late error from a job that was queued before disconnecting
        text = str(exc) if isinstance(exc, ModbusError) else f"{type(exc).__name__}: {exc}"
        prefix = f"{what}: " if what else ""
        self._append_log("err", prefix + text, time.time())
        self._status(prefix + (text.splitlines() or [""])[0], error=True)
        if isinstance(exc, LinkLost):
            self._disconnect(reason=text)
            messagebox.showerror("Mất kết nối", text, parent=self.root)

    def _status(self, text: str, error: bool = False) -> None:
        self.status_var.set(text)
        self.status_label.configure(style="StatusErr.TLabel" if error else "Status.TLabel")

    def _read_int(self, var: tk.StringVar, low: int, high: int, label: str) -> int:
        try:
            value = parse_int(var.get())
        except ValueError:
            raise ValueError(f"{label}: '{var.get()}' không phải là số") from None
        if not low <= value <= high:
            raise ValueError(f"{label} phải trong khoảng {low}..{high}")
        return value

    # ------------------------------------------------------------------ layout

    def px(self, size: float) -> int:
        """Pixel size at 96 DPI, scaled to the screen (Windows display scaling)."""
        return int(round(size * self.scale))

    def _init_style(self) -> None:
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        base = tkfont.nametofont("TkDefaultFont")
        family = base.cget("family")
        self.mono = tkfont.Font(family="Consolas", size=10)
        self.value_font = tkfont.Font(family=family, size=22, weight="bold")
        style.configure("Value.TLabel", font=self.value_font, foreground="#0b3d91")
        style.configure("Unit.TLabel", foreground="#555555")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Header.TLabel", font=(family, 9, "bold"))
        style.configure("Code.TLabel", font=self.mono)
        style.configure("Current.TLabel", font=(family, 10, "bold"), foreground="#0b3d91")
        style.configure("On.TLabel", foreground="#1b7f2a", font=(family, 10, "bold"))
        style.configure("Off.TLabel", foreground="#9a9a9a", font=(family, 10, "bold"))
        style.configure("Status.TLabel", foreground="#333333")
        style.configure("StatusErr.TLabel", foreground="#b00020")
        style.configure("Danger.TButton", foreground="#b00020")
        style.configure("Treeview", rowheight=int(base.metrics("linespace") * 1.45))

    def _build(self) -> None:
        self.root.title(f"DDSU666 — đọc & cài đặt thông số qua RS485   v{__version__}")
        width = min(self.px(1180), self.root.winfo_screenwidth() - 40)
        height = min(self.px(840), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(self.px(1000), width), min(self.px(680), height))

        self.status_var = tk.StringVar(value="Sẵn sàng")
        bar = ttk.Frame(self.root, padding=(8, 2))
        bar.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel", anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

        self._build_connection(self.root)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        for title, builder in (("  Đo lường  ", self._build_measure_tab),
                               ("  Cài đặt thông số  ", self._build_settings_tab),
                               ("  Thanh ghi thô  ", self._build_raw_tab),
                               ("  Quét thiết bị  ", self._build_scan_tab),
                               ("  Nhật ký  ", self._build_log_tab)):
            frame = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(frame, text=title)
            builder(frame)

    def _build_connection(self, parent) -> None:
        settings = self.settings
        box = ttk.LabelFrame(parent, text="Kết nối", padding=(8, 4, 8, 6))
        box.pack(fill="x", padx=8, pady=(8, 6))
        self.mode_var = tk.StringVar(value=LABEL_BY_MODE.get(settings["mode"], MODES[0][1]))
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=str(settings["baud"]))
        self.fmt_var = tk.StringVar(value=settings["fmt"] if settings["fmt"] in R.FRAME_FORMATS else "8N1")
        self.slave_var = tk.StringVar(value=str(settings["slave"]))
        self.timeout_var = tk.StringVar(value=str(settings["timeout_ms"]))
        self.retries_var = tk.StringVar(value=str(settings["retries"]))
        self.order_var = tk.StringVar(value=settings["word_order"] if settings["word_order"] in ("ABCD", "CDAB")
                                      else "ABCD")

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Chế độ:").pack(side="left")
        self.mode_box = ttk.Combobox(row, textvariable=self.mode_var, values=[label for _, label in MODES],
                                     state="readonly", width=40)
        self.mode_box.pack(side="left", padx=(4, 14))
        self.mode_box.bind("<<ComboboxSelected>>", lambda _event: self._mode_changed())
        ttk.Label(row, text="Cổng COM:").pack(side="left")
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, width=42)
        self.port_box.pack(side="left", padx=4)
        self.refresh_button = ttk.Button(row, text="↻", width=3, command=self._refresh_ports)
        self.refresh_button.pack(side="left", padx=(0, 14))
        self.connect_button = ttk.Button(row, text="Kết nối", width=16, command=self._toggle_connect)
        self.connect_button.pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Tốc độ:").pack(side="left")
        baud_box = ttk.Combobox(row, textvariable=self.baud_var, values=[str(b) for b in R.LINE_BAUDS],
                                state="readonly", width=7)
        baud_box.pack(side="left", padx=(4, 2))
        baud_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_line_settings())
        ttk.Label(row, text="bps").pack(side="left", padx=(0, 14))
        ttk.Label(row, text="Khung:").pack(side="left")
        fmt_box = ttk.Combobox(row, textvariable=self.fmt_var, values=R.FRAME_FORMATS, state="readonly", width=5)
        fmt_box.pack(side="left", padx=(4, 14))
        fmt_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_line_settings())
        ttk.Label(row, text="Địa chỉ đồng hồ:").pack(side="left")
        slave_box = ttk.Spinbox(row, textvariable=self.slave_var, from_=1, to=247, width=5,
                                command=self._apply_client_settings)
        slave_box.pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Timeout (ms):").pack(side="left")
        timeout_box = ttk.Spinbox(row, textvariable=self.timeout_var, from_=50, to=5000, increment=50, width=6,
                                  command=self._apply_client_settings)
        timeout_box.pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Thử lại:").pack(side="left")
        retries_box = ttk.Spinbox(row, textvariable=self.retries_var, from_=0, to=5, width=3,
                                  command=self._apply_client_settings)
        retries_box.pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Thứ tự word float:").pack(side="left")
        order_box = ttk.Combobox(row, textvariable=self.order_var, values=("ABCD", "CDAB"), state="readonly",
                                 width=6)
        order_box.pack(side="left", padx=4)
        order_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_client_settings())
        for widget in (slave_box, timeout_box, retries_box):
            widget.bind("<Return>", lambda _event: self._apply_client_settings())
            widget.bind("<FocusOut>", lambda _event: self._apply_client_settings())
        self.line_widgets = [baud_box, fmt_box, slave_box]

        self.conn_label = ttk.Label(box, text="● Chưa kết nối", style="Off.TLabel")
        self.conn_label.pack(anchor="w", pady=(6, 0))
        ttk.Label(box, style="Hint.TLabel",
                  text="Mặc định đồng hồ: 9600 bps, 8N1. Định dạng khung chỉ đổi được bằng nút trên đồng hồ — "
                       "chọn ở đây cho khớp. Không biết địa chỉ/tốc độ? Dùng tab Quét thiết bị.").pack(
            anchor="w", pady=(4, 0))

    def _build_measure_tab(self, tab) -> None:
        tiles = ttk.Frame(tab)
        tiles.pack(fill="x")
        self.tiles: dict = {}
        for index, (key, title, unit, digits) in enumerate(TILES):
            tile = Tile(tiles, title, unit, digits)
            tile.frame.grid(row=index // 3, column=index % 3, sticky="nsew", padx=4, pady=3)
            self.tiles[key] = tile
        for column in range(3):
            tiles.columnconfigure(column, weight=1, uniform="tile")

        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(8, 6))
        button = ttk.Button(controls, text="Đọc ngay", command=self._read_measurements)
        button.pack(side="left")
        self.needs_connection.append(button)
        self.poll_var = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(controls, text="Tự động đọc mỗi", variable=self.poll_var,
                                command=self._toggle_poll)
        check.pack(side="left", padx=(14, 4))
        self.needs_connection.append(check)
        self.poll_ms_var = tk.StringVar(value=str(self.settings["poll_ms"]))
        ttk.Spinbox(controls, textvariable=self.poll_ms_var, from_=200, to=3600000, increment=100,
                    width=7).pack(side="left")
        ttk.Label(controls, text="ms").pack(side="left", padx=(2, 18))
        self.csv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Ghi CSV", variable=self.csv_var, command=self._toggle_csv).pack(side="left")
        self.csv_path_var = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.csv_path_var, style="Hint.TLabel").pack(side="left", padx=6)
        self.update_var = tk.StringVar(value="Chưa đọc")
        ttk.Label(controls, textvariable=self.update_var, style="Hint.TLabel").pack(side="right")

        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True)
        columns = ("addr", "code", "name", "raw", "value", "unit", "status")
        self.measure_tree = ttk.Treeview(table, columns=columns, show="headings", height=12)
        for column, title, width, anchor in (("addr", "Địa chỉ", 80, "center"), ("code", "Mã", 90, "w"),
                                             ("name", "Mô tả", 330, "w"), ("raw", "Thanh ghi (hex)", 130, "center"),
                                             ("value", "Giá trị", 150, "e"), ("unit", "Đơn vị", 70, "center"),
                                             ("status", "Ghi chú", 220, "w")):
            self.measure_tree.heading(column, text=title)
            self.measure_tree.column(column, width=self.px(width), anchor=anchor,
                                     stretch=column in ("name", "status"))
        self.measure_tree.tag_configure("reserved", foreground="#888888")
        self.measure_tree.tag_configure("error", foreground="#b00020")
        for register in R.MEASUREMENTS + R.ENERGY:
            self.measure_tree.insert("", "end", iid=f"m{register.address:04X}", tags=self._tags(register),
                                     values=(f"{register.address:04X}H", register.code, register.name, "", "—",
                                             register.unit, "RESERVED trong tài liệu" if register.reserved else ""))
        self.measure_tree.insert("", "end", iid="mComEp", values=(
            "—", "ComEp", "Điện năng tổng hợp (tính = Ep + (−Ep), như ví dụ Bảng 8)", "", "—", "kWh", "tính toán"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.measure_tree.yview)
        self.measure_tree.configure(yscrollcommand=scroll.set)
        self.measure_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    @staticmethod
    def _wrapping_label(parent, text: str = "", **options) -> ttk.Label:
        """A left-aligned label that re-wraps its text to whatever width it gets."""
        label = ttk.Label(parent, text=text, justify="left", anchor="nw", **options)
        label.pack(fill="both", expand=True, anchor="nw")
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(100, event.width - 8)))
        return label

    @staticmethod
    def _tags(register: R.Register) -> tuple:
        return ("reserved",) if register.reserved else ()

    def _build_settings_tab(self, tab) -> None:
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        button = ttk.Button(bar, text="Đọc tất cả thông số", command=self._read_parameters)
        button.pack(side="left")
        self.needs_connection.append(button)
        self.params_time_var = tk.StringVar(value="Chưa đọc")
        ttk.Label(bar, textvariable=self.params_time_var, style="Hint.TLabel").pack(side="left", padx=10)

        grid = ttk.LabelFrame(tab, text="Thông số truyền thông — Bảng 9 trong tài liệu", padding=(8, 4))
        grid.pack(fill="x")
        for column, title in enumerate(("Địa chỉ", "Mã", "Mô tả", "Quyền", "Giá trị hiện tại", "Giá trị mới", "")):
            ttk.Label(grid, text=title, style="Header.TLabel").grid(row=0, column=column, sticky="w",
                                                                   padx=6, pady=(0, 4))
        grid.columnconfigure(2, weight=1)
        self.param_vars: dict[int, tk.StringVar] = {}
        row = 1
        for register in R.PARAMETERS:
            if register.reserved:
                continue
            ttk.Label(grid, text=f"{register.address:04X}H", style="Code.TLabel").grid(
                row=row, column=0, sticky="w", padx=6, pady=3)
            ttk.Label(grid, text=register.code, style="Code.TLabel").grid(row=row, column=1, sticky="w", padx=6)
            ttk.Label(grid, text=register.name, wraplength=self.px(300)).grid(row=row, column=2, sticky="w",
                                                                             padx=6)
            ttk.Label(grid, text=register.access).grid(row=row, column=3, sticky="w", padx=6)
            var = tk.StringVar(value="—")
            self.param_vars[register.address] = var
            ttk.Label(grid, textvariable=var, style="Current.TLabel", width=21).grid(row=row, column=4,
                                                                                    sticky="w", padx=6)
            editor, action = self._param_editor(grid, register)
            if editor is not None:
                editor.grid(row=row, column=5, sticky="w", padx=6)
            if action is not None:
                action.grid(row=row, column=6, sticky="w", padx=6)
                self.needs_connection.append(action)
            row += 1

        bottom = ttk.Frame(tab)
        bottom.pack(fill="both", expand=True, pady=(8, 0))
        reserved_box = ttk.LabelFrame(bottom, text="Thanh ghi dự phòng (RESERVED) trong Bảng 9", padding=6)
        reserved_box.pack(side="left", fill="y")
        self.reserved_tree = ttk.Treeview(reserved_box, columns=("addr", "raw", "value", "note"), show="headings",
                                          height=10)
        for column, title, width in (("addr", "Địa chỉ", 70), ("raw", "Hex", 60), ("value", "Int16", 70),
                                     ("note", "Ghi chú", 150)):
            self.reserved_tree.heading(column, text=title)
            self.reserved_tree.column(column, width=self.px(width), anchor="center" if column != "note" else "w")
        self.reserved_tree.tag_configure("error", foreground="#b00020")
        for register in R.PARAMETERS:
            if register.reserved:
                self.reserved_tree.insert("", "end", iid=f"p{register.address:04X}",
                                          values=(f"{register.address:04X}H", "", "—", "chỉ hiển thị"))
        scroll = ttk.Scrollbar(reserved_box, orient="vertical", command=self.reserved_tree.yview)
        self.reserved_tree.configure(yscrollcommand=scroll.set)
        self.reserved_tree.pack(side="left", fill="y", expand=True)
        scroll.pack(side="left", fill="y")

        notes = ttk.LabelFrame(bottom, text="Lưu ý khi cài đặt", padding=8)
        notes.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._wrapping_label(notes, NOTES)

    def _param_editor(self, parent, register: R.Register):
        if register is R.UCODE:
            self.ucode_new = tk.StringVar()
            return (ttk.Entry(parent, textvariable=self.ucode_new, width=14),
                    ttk.Button(parent, text="Ghi", command=self._write_ucode))
        if register is R.CLR_E:
            return None, ttk.Button(parent, text="Xóa điện năng…", style="Danger.TButton",
                                    command=self._clear_energy)
        if register is R.CHANGE_PROTOCOL:
            self.protocol_new = tk.StringVar(value=f"2 — {R.CHANGE_PROTOCOL.choice_label(2)}")
            box = ttk.Combobox(parent, textvariable=self.protocol_new, state="readonly", width=19,
                               values=[f"{value} — {label}" for value, label in R.PROTOCOL_CHOICES])
            return box, ttk.Button(parent, text="Ghi", command=self._write_protocol)
        if register is R.ADDR:
            self.addr_new = tk.StringVar(value=str(self.settings["slave"]))
            return (ttk.Spinbox(parent, textvariable=self.addr_new, from_=1, to=247, width=12),
                    ttk.Button(parent, text="Ghi", command=self._write_address))
        if register is R.BAUD:
            self.baud_new = tk.StringVar(value="3 — 9600 bps")
            box = ttk.Combobox(parent, textvariable=self.baud_new, state="readonly", width=19,
                               values=[f"{value} — {label}" for value, label in R.BAUD_CHOICES])
            return box, ttk.Button(parent, text="Ghi", command=self._write_baud)
        return None, None

    def _build_raw_tab(self, tab) -> None:
        read_box = ttk.LabelFrame(tab, text="Đọc thanh ghi — lệnh 03H", padding=8)
        read_box.pack(fill="both", expand=True)
        row = ttk.Frame(read_box)
        row.pack(fill="x")
        self.raw_start = tk.StringVar(value="2000H")
        self.raw_count = tk.StringVar(value="18")
        ttk.Label(row, text="Địa chỉ bắt đầu:").pack(side="left")
        ttk.Entry(row, textvariable=self.raw_start, width=10).pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Số thanh ghi:").pack(side="left")
        ttk.Spinbox(row, textvariable=self.raw_count, from_=1, to=125, width=5).pack(side="left", padx=(4, 14))
        button = ttk.Button(row, text="Đọc", command=self._raw_read)
        button.pack(side="left")
        self.needs_connection.append(button)
        ttk.Label(read_box, style="Hint.TLabel",
                  text="Nhập 2000H, 0x2000 hoặc 8192. Vùng có trong tài liệu: 0000H–0010H, 2000H–2011H, "
                       "4000H–4001H, 400AH–400BH.").pack(anchor="w", pady=(4, 0))
        table = ttk.Frame(read_box)
        table.pack(fill="both", expand=True, pady=(6, 0))
        columns = ("addr", "code", "hex", "uint", "int", "abcd", "cdab")
        self.raw_tree = ttk.Treeview(table, columns=columns, show="headings", height=6)
        for column, title, width in (("addr", "Địa chỉ", 80), ("code", "Mã (tài liệu)", 190), ("hex", "Hex", 70),
                                     ("uint", "UInt16", 80), ("int", "Int16", 80),
                                     ("abcd", "Float32 ABCD (từ ô này)", 180),
                                     ("cdab", "Float32 CDAB (từ ô này)", 180)):
            self.raw_tree.heading(column, text=title)
            self.raw_tree.column(column, width=self.px(width), anchor="center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.raw_tree.yview)
        self.raw_tree.configure(yscrollcommand=scroll.set)
        self.raw_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        write_box = ttk.LabelFrame(tab, text="Ghi thanh ghi — lệnh 10H", padding=8)
        write_box.pack(fill="x", pady=(8, 0))
        self.raw_wstart = tk.StringVar(value="0006H")
        self.raw_values = tk.StringVar(value="1")
        ttk.Label(write_box, text="Địa chỉ bắt đầu:").pack(side="left")
        ttk.Entry(write_box, textvariable=self.raw_wstart, width=10).pack(side="left", padx=(4, 14))
        ttk.Label(write_box, text="Giá trị (cách nhau bằng dấu phẩy, thập phân hoặc 0x..):").pack(side="left")
        ttk.Entry(write_box, textvariable=self.raw_values, width=30).pack(side="left", padx=(4, 14))
        button = ttk.Button(write_box, text="Ghi…", command=self._raw_write)
        button.pack(side="left")
        self.needs_connection.append(button)

        frame_box = ttk.LabelFrame(tab, text="Gửi khung tùy ý (hex)", padding=8)
        frame_box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(frame_box)
        row.pack(fill="x")
        self.raw_frame = tk.StringVar(value="01 03 00 0C 00 02")
        self.raw_crc = tk.BooleanVar(value=True)
        ttk.Label(row, text="Khung:").pack(side="left")
        ttk.Entry(row, textvariable=self.raw_frame, width=48, font=self.mono).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(row, text="Tự thêm CRC", variable=self.raw_crc).pack(side="left", padx=(0, 10))
        button = ttk.Button(row, text="Gửi", command=self._raw_send)
        button.pack(side="left")
        self.needs_connection.append(button)
        self.raw_result = tk.StringVar(value="Ví dụ trong tài liệu (Bảng A.3): 01 03 00 0C 00 02 + CRC 04 08.")
        result = self._wrapping_label(frame_box, textvariable=self.raw_result, font=self.mono)
        result.pack_configure(pady=(6, 0))

    def _build_scan_tab(self, tab) -> None:
        box = ttk.LabelFrame(tab, text="Phạm vi quét (chỉ dùng lệnh đọc 03H — không thay đổi đồng hồ)", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Tốc độ:", width=9).pack(side="left")
        self.scan_baud_vars = {}
        for baud in R.LINE_BAUDS:
            var = tk.BooleanVar(value=baud == 9600)
            self.scan_baud_vars[baud] = var
            ttk.Checkbutton(row, text=f"{baud} bps", variable=var, command=self._scan_estimate).pack(
                side="left", padx=(0, 10))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Khung:", width=9).pack(side="left")
        self.scan_fmt_vars = {}
        for fmt in R.FRAME_FORMATS:
            var = tk.BooleanVar(value=fmt == "8N1")
            self.scan_fmt_vars[fmt] = var
            ttk.Checkbutton(row, text=fmt, variable=var, command=self._scan_estimate).pack(side="left", padx=(0, 10))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        self.scan_from = tk.StringVar(value="1")
        self.scan_to = tk.StringVar(value="247")
        self.scan_wait = tk.StringVar(value="150")
        self.scan_stop = tk.BooleanVar(value=True)
        ttk.Label(row, text="Địa chỉ từ", width=9).pack(side="left")
        for var in (self.scan_from, self.scan_to):
            spin = ttk.Spinbox(row, textvariable=var, from_=1, to=247, width=5, command=self._scan_estimate)
            spin.pack(side="left", padx=(0, 6))
            spin.bind("<KeyRelease>", lambda _event: self._scan_estimate())
            if var is self.scan_from:
                ttk.Label(row, text="đến").pack(side="left", padx=(0, 6))
        ttk.Label(row, text="   Chờ mỗi địa chỉ (ms):").pack(side="left")
        spin = ttk.Spinbox(row, textvariable=self.scan_wait, from_=50, to=2000, increment=10, width=6,
                           command=self._scan_estimate)
        spin.pack(side="left", padx=(4, 14))
        spin.bind("<KeyRelease>", lambda _event: self._scan_estimate())
        ttk.Checkbutton(row, text="Dừng khi tìm thấy đồng hồ", variable=self.scan_stop).pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.scan_button = ttk.Button(row, text="Bắt đầu quét", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.needs_connection.append(self.scan_button)
        self.scan_stop_button = ttk.Button(row, text="Dừng", command=self.scan_cancel.set, state="disabled")
        self.scan_stop_button.pack(side="left", padx=8)
        self.scan_estimate_var = tk.StringVar()
        ttk.Label(row, textvariable=self.scan_estimate_var, style="Hint.TLabel").pack(side="left", padx=8)

        self.scan_progress = ttk.Progressbar(tab, mode="determinate")
        self.scan_progress.pack(fill="x", pady=(10, 2))
        self.scan_status = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.scan_status, style="Hint.TLabel").pack(anchor="w")

        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True, pady=(6, 0))
        columns = ("baud", "fmt", "addr", "outcome", "rev", "type", "detail")
        self.scan_tree = ttk.Treeview(table, columns=columns, show="headings", height=8)
        for column, title, width in (("baud", "Tốc độ", 80), ("fmt", "Khung", 60), ("addr", "Địa chỉ", 70),
                                     ("outcome", "Kết quả", 150), ("rev", "REV.", 110), ("type", "Meter type", 110),
                                     ("detail", "Chi tiết", 360)):
            self.scan_tree.heading(column, text=title)
            self.scan_tree.column(column, width=self.px(width), anchor="w" if column == "detail" else "center",
                                  stretch=column == "detail")
        self.scan_tree.tag_configure("ok", foreground="#1b7f2a")
        self.scan_tree.tag_configure("bad", foreground="#a05a00")
        self.scan_tree.bind("<Double-1>", lambda _event: self._use_scan_hit())
        self.scan_tree.pack(fill="both", expand=True)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Dùng thiết lập đã chọn", command=self._use_scan_hit).pack(side="left")
        ttk.Label(row, style="Hint.TLabel",
                  text="(hoặc nhấp đúp vào một dòng) — đặt tốc độ, khung và địa chỉ ở khung Kết nối.").pack(
            side="left", padx=8)
        self.scan_hits: dict[str, ScanHit] = {}
        self._scan_estimate()

    def _build_log_tab(self, tab) -> None:
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        self.log_frames = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Hiện khung TX/RX", variable=self.log_frames).pack(side="left")
        ttk.Button(bar, text="Xóa", command=self._clear_log).pack(side="left", padx=8)
        ttk.Button(bar, text="Lưu ra file…", command=self._save_log).pack(side="left")
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, font=self.mono, wrap="none", state="disabled", height=20,
                                background="#fbfbfb")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for tag, color in (("tx", "#0b3d91"), ("rx", "#1b7f2a"), ("info", "#555555"), ("warn", "#a05a00"),
                           ("err", "#b00020"), ("dev", "#6a1b9a")):
            self.log_text.tag_configure(tag, foreground=color)

    # ------------------------------------------------------------------ connection

    def _mode_key(self) -> str:
        return MODE_BY_LABEL.get(self.mode_var.get(), "bridge")

    def _mode_changed(self) -> None:
        sim = self._mode_key() == "sim"
        for widget in (self.port_box, self.refresh_button):
            widget.state(["disabled"] if sim or self.connected else ["!disabled"])

    def _refresh_ports(self) -> None:
        try:
            ports = list_serial_ports()
        except Exception as exc:
            ports = []
            self._append_log("warn", f"Không liệt kê được cổng COM: {exc}", time.time())
        values = [f"{device} — {description}" if description else device for device, description in ports]
        self.port_box.configure(values=values)
        current = self._selected_port()
        devices = [device for device, _ in ports]
        if current in devices:
            self.port_var.set(values[devices.index(current)])
        elif self.settings["port"] in devices:
            self.port_var.set(values[devices.index(self.settings["port"])])
        elif values and not current:
            self.port_var.set(values[0])
        self._status(f"Tìm thấy {len(values)} cổng COM" if values else "Không tìm thấy cổng COM nào")

    def _selected_port(self) -> str:
        text = self.port_var.get().strip()
        return text.split()[0] if text else ""

    def _set_connected(self, connected: bool, description: str = "") -> None:
        self.connected = connected
        self.connect_button.configure(text="Ngắt kết nối" if connected else "Kết nối")
        self.connect_button.state(["!disabled"])
        if connected:
            self.conn_label.configure(text=f"● Đã kết nối — {description}", style="On.TLabel")
        else:
            self.conn_label.configure(text="● Chưa kết nối", style="Off.TLabel")
        self.mode_box.state(["disabled"] if connected else ["!disabled"])
        self._mode_changed()
        for widget in self.needs_connection:
            widget.state(["!disabled"] if connected else ["disabled"])
        if self.scanning:
            self.scan_button.state(["disabled"])

    def _toggle_connect(self) -> None:
        if self.connected:
            self._disconnect()
            return
        if self.connecting:
            return
        mode = self._mode_key()
        port = self._selected_port()
        if mode != "sim" and not port:
            messagebox.showwarning("Chưa chọn cổng", "Chọn cổng COM của ESP32 (hoặc bộ USB-RS485).",
                                   parent=self.root)
            return
        try:
            slave = self._read_int(self.slave_var, 1, 247, "Địa chỉ đồng hồ")
            timeout = self._read_int(self.timeout_var, 50, 5000, "Timeout")
            retries = self._read_int(self.retries_var, 0, 5, "Số lần thử lại")
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        baud, fmt = int(self.baud_var.get()), self.fmt_var.get()
        log = self._log_any_thread
        if mode == "bridge":
            transport = BridgeTransport(port, baud, fmt, log=log)
        elif mode == "direct":
            transport = DirectTransport(port, baud, fmt, log=log)
        else:
            if self.sim_meter is None:
                self.sim_meter = SimulatedDDSU666()
            transport = SimTransport(self.sim_meter, baud, fmt, log=log)
        client = DDSU666Client(transport, slave, timeout, retries, self.order_var.get(), log=log)
        self.connecting = True
        self.connect_button.state(["disabled"])
        self.mode_box.state(["disabled"])

        def opened(description: str) -> None:
            self.connecting = False
            self.transport, self.client = transport, client
            self._set_connected(True, description)
            self._append_log("info", f"Đã kết nối: {description}", time.time())
            self._status(f"Đã kết nối — {description}. Đang đọc thông số…")
            self._read_parameters()
            self._read_measurements()

        def failed(exc: Exception) -> None:
            self.connecting = False
            self._set_connected(False)
            self._append_log("err", f"Kết nối thất bại: {exc}", time.time())
            self._status("Kết nối thất bại", error=True)
            messagebox.showerror("Không kết nối được", str(exc), parent=self.root)

        where = "bộ mô phỏng" if mode == "sim" else port
        self.run(transport.open, opened, failed, status=f"Đang kết nối {where}…")

    def _disconnect(self, reason: str = "") -> None:
        self.poll_var.set(False)
        self.scan_cancel.set()
        transport = self.transport
        self.transport = None
        self.client = None
        self._set_connected(False)
        if transport is not None:
            self.worker.submit(transport.close)
        self._status("Đã ngắt kết nối" + (f": {reason.splitlines()[0]}" if reason else ""), error=bool(reason))

    def _apply_line_settings(self) -> None:
        if not self.connected or self.scanning:
            return
        baud, fmt = int(self.baud_var.get()), self.fmt_var.get()
        transport = self.transport
        self.run(lambda: transport.configure(baud, fmt),
                 lambda _: self._status(f"Đường truyền: {baud} bps {fmt}"),
                 lambda exc: self._job_failed(exc, "Đổi tốc độ/khung"))

    def _apply_client_settings(self) -> None:
        if not self.connected:
            return
        try:
            slave = self._read_int(self.slave_var, 1, 247, "Địa chỉ đồng hồ")
            timeout = self._read_int(self.timeout_var, 50, 5000, "Timeout")
            retries = self._read_int(self.retries_var, 0, 5, "Số lần thử lại")
        except ValueError as exc:
            self._status(str(exc), error=True)
            return
        client, order = self.client, self.order_var.get()

        def apply() -> None:
            client.slave, client.timeout_ms, client.retries, client.word_order = slave, timeout, retries, order

        self.run(apply)

    # ------------------------------------------------------------------ measurements

    def _read_measurements(self, from_poll: bool = False) -> None:
        if not self.connected:
            if from_poll:
                self.poll_inflight = False
            return
        client = self.client
        started = time.monotonic()

        def done(readings: list[Reading]) -> None:
            if self.connected:
                self._show_measurements(readings, time.monotonic() - started)
            if from_poll:
                self._poll_done()

        def failed(exc: Exception) -> None:
            self._job_failed(exc, "Đọc đo lường")
            if from_poll:
                self.poll_errors += 1
                self._poll_done()

        self.run(client.read_measurements, done, failed)

    def _show_measurements(self, readings: list[Reading], elapsed: float) -> None:
        by_address = {reading.register.address: reading for reading in readings}
        for key, tile in self.tiles.items():
            if key == "ComEp":
                continue
            reading = by_address.get(key)
            value = reading.value if reading is not None and reading.ok else None
            extra = ""
            if key == R.ACTIVE_POWER.address and value is not None:
                extra = f"= {value * 1000:.1f} W"
            elif reading is not None and reading.error:
                extra = reading.error[:40]
            tile.set(value, extra)
        total = combined_energy(readings)
        self.tiles["ComEp"].set(total, "= Ep + (−Ep)")
        for reading in readings:
            register = reading.register
            digits = DECIMALS.get(register.address, 4)
            value = fmt_number(reading.value, digits) if reading.ok else "—"
            status = reading.error or ("RESERVED trong tài liệu" if register.reserved else "")
            tags = ("error",) if reading.error else self._tags(register)
            self.measure_tree.item(f"m{register.address:04X}", tags=tags, values=(
                f"{register.address:04X}H", register.code, register.name, reading.raw_hex, value, register.unit,
                status))
        self.measure_tree.set("mComEp", "value", fmt_number(total, 2))
        stamp = time.strftime("%H:%M:%S")
        errors = f" · lỗi khi tự đọc: {self.poll_errors}" if self.poll_errors else ""
        self.update_var.set(f"Cập nhật {stamp} ({elapsed * 1000:.0f} ms){errors}")
        if not self.poll_var.get():
            self._status(f"Đã đọc thông số đo lúc {stamp}")
        if self.csv_writer is not None:
            row = [time.strftime("%Y-%m-%d %H:%M:%S")]
            for _, address in CSV_COLUMNS:
                reading = by_address.get(address)
                row.append(f"{reading.value:.6g}" if reading is not None and reading.ok else "")
            row.append(f"{total:.6g}" if total is not None else "")
            try:
                self.csv_writer.writerow(row)
                self.csv_file.flush()
            except OSError as exc:
                self._append_log("err", f"Không ghi được CSV: {exc}", time.time())
                self._close_csv()

    def _poll_interval_ms(self) -> int:
        try:
            return max(200, parse_int(self.poll_ms_var.get()))
        except ValueError:
            return 1000

    def _toggle_poll(self) -> None:
        if self.poll_var.get():
            self.poll_errors = 0
            self._poll_tick()

    def _poll_tick(self) -> None:
        if not self.poll_var.get() or not self.connected or self.poll_inflight or self.scanning:
            return
        self.poll_inflight = True
        self.poll_started = time.monotonic()
        self._read_measurements(from_poll=True)

    def _poll_done(self) -> None:
        self.poll_inflight = False
        if self.poll_var.get() and self.connected:
            elapsed_ms = int((time.monotonic() - self.poll_started) * 1000)
            self.root.after(max(20, self._poll_interval_ms() - elapsed_ms), self._poll_tick)

    def _toggle_csv(self) -> None:
        if not self.csv_var.get():
            self._close_csv()
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Ghi số đo ra CSV", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], initialfile=f"ddsu666_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            self.csv_var.set(False)
            return
        try:
            is_new = not os.path.exists(path) or os.path.getsize(path) == 0
            self.csv_file = open(path, "a", newline="", encoding="utf-8-sig")
            self.csv_writer = csv.writer(self.csv_file)
            if is_new:
                self.csv_writer.writerow(["Thời gian"] + [title for title, _ in CSV_COLUMNS] + ["ComEp (kWh)"])
        except OSError as exc:
            messagebox.showerror("Không mở được file", str(exc), parent=self.root)
            self._close_csv()
            return
        self.csv_path_var.set(os.path.basename(path))

    def _close_csv(self) -> None:
        if self.csv_file is not None:
            try:
                self.csv_file.close()
            except OSError:
                pass
        self.csv_file = None
        self.csv_writer = None
        self.csv_var.set(False)
        self.csv_path_var.set("")

    # ------------------------------------------------------------------ parameters (table 9)

    def _read_parameters(self) -> None:
        if not self.connected:
            return
        self.run(self.client.read_parameters, self._show_parameters,
                 lambda exc: self._job_failed(exc, "Đọc thông số"))

    def _show_parameters(self, readings: list[Reading]) -> None:
        for reading in readings:
            register = reading.register
            if register.reserved:
                if reading.ok:
                    values = (f"{register.address:04X}H", reading.raw_hex, reading.value, "")
                    tags = ()
                else:
                    values = (f"{register.address:04X}H", "", "—", reading.error or "")
                    tags = ("error",)
                self.reserved_tree.item(f"p{register.address:04X}", values=values, tags=tags)
                continue
            var = self.param_vars[register.address]
            if not reading.ok:
                var.set(f"lỗi: {reading.error}"[:60])
                continue
            value = int(reading.value)
            if register.choices:
                label = register.choice_label(value)
                var.set(f"{value} — {label}" if label else f"{value} (không có trong tài liệu)")
            elif register in (R.REV, R.METER_TYPE):
                var.set(fmt_word(value))
            else:
                var.set(str(value))
            # Start every editor from the value the meter holds now.
            if register is R.UCODE:
                self.ucode_new.set(str(value))
            elif register is R.ADDR:
                self.addr_new.set(str(value))
            elif register is R.CHANGE_PROTOCOL and register.choice_label(value):
                self.protocol_new.set(f"{value} — {register.choice_label(value)}")
            elif register is R.BAUD and register.choice_label(value):
                self.baud_new.set(f"{value} — {register.choice_label(value)}")
        stamp = time.strftime("%H:%M:%S")
        self.params_time_var.set(f"Đọc lúc {stamp}")
        self._status(f"Đã đọc thông số Bảng 9 lúc {stamp}")

    def _write_failed(self, exc: Exception, what: str) -> None:
        self._job_failed(exc, what)
        if self.connected and not isinstance(exc, LinkLost):
            messagebox.showerror(f"{what} thất bại", str(exc), parent=self.root)

    def _written(self, message: str, reread_measurements: bool = False) -> None:
        self._append_log("info", message, time.time())
        self._status(message)
        self._read_parameters()
        if reread_measurements:
            self._read_measurements()

    def _confirm(self, title: str, text: str, warning: bool = False) -> bool:
        return messagebox.askyesno(title, text, icon="warning" if warning else "question",
                                   default="no" if warning else "yes", parent=self.root)

    def _write_ucode(self) -> None:
        if not self.connected:
            return
        try:
            value = self._read_int(self.ucode_new, -32768, 32767, "UCode")
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        if not self._confirm("Ghi UCode", f"Ghi mật khẩu lập trình UCode (0000H) = {value}?"):
            return
        client = self.client
        self.run(lambda: client.write_parameter(R.UCODE, value),
                 lambda _: self._written(f"Đã ghi UCode = {value}"),
                 lambda exc: self._write_failed(exc, "Ghi UCode"))

    def _clear_energy(self) -> None:
        if not self.connected:
            return
        if not self._confirm("Xóa điện năng",
                             "Ghi ClrE (0002H) = 1 sẽ XÓA điện năng tích lũy của đồng hồ về 0.\n"
                             "Thao tác này KHÔNG hoàn tác được.\n\nTiếp tục?", warning=True):
            return
        client = self.client
        self.run(client.clear_energy, lambda _: self._written("Đã xóa điện năng (ClrE = 1)", True),
                 lambda exc: self._write_failed(exc, "Xóa điện năng"))

    def _write_protocol(self) -> None:
        if not self.connected:
            return
        value = int(self.protocol_new.get().split()[0])
        if value == 1:
            ok = self._confirm("Chuyển sang DL/T 645-2007",
                               "Sau lệnh này đồng hồ KHÔNG trả lời Modbus nữa — app sẽ mất liên lạc với nó.\n\n"
                               "Muốn quay lại Modbus phải nhấn giữ nút trên đồng hồ rồi chọn một định dạng "
                               "8n1/8n2/8E1/8o1 (Hình 3 trong tài liệu).\n\nVẫn chuyển?", warning=True)
        else:
            ok = self._confirm("Ghi ChangeProtocol", "Ghi ChangeProtocol (0005H) = 2 (Modbus RTU)?")
        if not ok:
            return
        client = self.client

        def done(message: str) -> None:
            self._append_log("info", message, time.time())
            self._status(message)
            if value == 1:
                self.poll_var.set(False)
                messagebox.showinfo("ChangeProtocol", message, parent=self.root)
            else:
                self._read_parameters()

        self.run(lambda: client.set_protocol(value), done, lambda exc: self._write_failed(exc, "Đổi giao thức"))

    def _write_address(self) -> None:
        if not self.connected:
            return
        try:
            new = self._read_int(self.addr_new, 1, 247, "Địa chỉ mới")
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        client = self.client
        note = "\n\nLưu ý: nút trên đồng hồ chỉ đặt được 1–99." if new > 99 else ""
        if not self._confirm("Đổi địa chỉ", f"Đổi địa chỉ Modbus của đồng hồ từ {client.slave} thành {new}?{note}"):
            return

        def done(message: str) -> None:
            self.slave_var.set(str(client.slave))
            self._written(message)

        def failed(exc: Exception) -> None:
            self.slave_var.set(str(client.slave))
            self._write_failed(exc, "Đổi địa chỉ")

        self.run(lambda: client.change_address(new), done, failed, status=f"Đang đổi địa chỉ thành {new}…")

    def _write_baud(self) -> None:
        if not self.connected:
            return
        code = int(self.baud_new.get().split()[0])
        rate = R.BAUD_RATES[code]
        if not self._confirm("Đổi tốc độ baud",
                             f"Ghi BAud (000CH) = {code} → đồng hồ chuyển sang {rate} bps.\n"
                             "App sẽ tự chuyển theo và đọc lại để xác nhận. Tiếp tục?"):
            return
        client, transport = self.client, self.transport

        def done(message: str) -> None:
            self.baud_var.set(str(transport.baud))
            self._written(message)

        def failed(exc: Exception) -> None:
            self.baud_var.set(str(transport.baud))
            self._write_failed(exc, "Đổi tốc độ baud")

        self.run(lambda: client.change_baud(code), done, failed, status=f"Đang đổi tốc độ sang {rate} bps…")

    # ------------------------------------------------------------------ raw registers

    def _raw_read(self) -> None:
        if not self.connected:
            return
        try:
            start = self._read_int(self.raw_start, 0, 0xFFFF, "Địa chỉ bắt đầu")
            count = self._read_int(self.raw_count, 1, 125, "Số thanh ghi")
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        client = self.client
        self.run(lambda: client.read_registers(start, count), lambda words: self._show_raw(start, words),
                 lambda exc: self._write_failed(exc, "Đọc thanh ghi"))

    def _show_raw(self, start: int, words: list[int]) -> None:
        self.raw_tree.delete(*self.raw_tree.get_children())
        for index, word in enumerate(words):
            address = start + index
            register = R.BY_ADDRESS.get(address)
            code = register.code if register is not None else ""
            if register is None and R.BY_ADDRESS.get(address - 1, None) is not None \
                    and R.BY_ADDRESS[address - 1].kind == R.FLOAT32:
                code = f"({R.BY_ADDRESS[address - 1].code} – word thấp)"
            abcd = cdab = ""
            if index + 1 < len(words):
                pair = (word, words[index + 1])
                abcd = f"{words_to_float(pair, 'ABCD'):.6g}"
                cdab = f"{words_to_float(pair, 'CDAB'):.6g}"
            signed = word - 0x10000 if word & 0x8000 else word
            self.raw_tree.insert("", "end", values=(f"{address:04X}H", code, f"{word:04X}", word, signed, abcd, cdab))
        self._status(f"Đã đọc {len(words)} thanh ghi từ {start:04X}H")

    def _raw_write(self) -> None:
        if not self.connected:
            return
        try:
            start = self._read_int(self.raw_wstart, 0, 0xFFFF, "Địa chỉ bắt đầu")
            parts = [part for part in self.raw_values.get().replace(";", ",").split(",") if part.strip()]
            values = [parse_int(part) for part in parts]
            if not values:
                raise ValueError("Chưa nhập giá trị")
            if any(not -32768 <= value <= 0xFFFF for value in values):
                raise ValueError("Mỗi giá trị phải nằm trong 16 bit (-32768..65535)")
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        span = range(start, start + len(values))
        risky = [register.code for register in (R.CLR_E, R.CHANGE_PROTOCOL, R.ADDR, R.BAUD)
                 if register.address in span]
        warning = ""
        if risky:
            warning = (f"\n\nVùng ghi có {', '.join(risky)}: ghi thô KHÔNG tự chuyển theo địa chỉ/tốc độ mới. "
                       "Nên dùng tab Cài đặt thông số cho các mục này.")
        listing = ", ".join(str(value) for value in values)
        if not self._confirm("Ghi thanh ghi", f"Ghi {len(values)} thanh ghi từ {start:04X}H: {listing}?{warning}",
                             warning=bool(risky)):
            return
        client = self.client
        self.run(lambda: client.write_registers(start, values),
                 lambda _: self._written(f"Đã ghi {len(values)} thanh ghi từ {start:04X}H"),
                 lambda exc: self._write_failed(exc, "Ghi thanh ghi"))

    def _raw_send(self) -> None:
        if not self.connected:
            return
        text = self.raw_frame.get().replace(" ", "").replace(",", "")
        try:
            frame = bytes.fromhex(text)
            if not frame:
                raise ValueError
        except ValueError:
            messagebox.showerror("Khung không hợp lệ", "Nhập các byte dạng hex, ví dụ: 01 03 00 0C 00 02",
                                 parent=self.root)
            return
        client, add_crc = self.client, self.raw_crc.get()

        def done(result) -> None:
            request, answer = result
            self.raw_result.set(f"Đã gửi: {request.hex(' ').upper()}\n{describe_answer(request, answer)}")

        self.run(lambda: client.raw_exchange(frame, add_crc), done,
                 lambda exc: self._write_failed(exc, "Gửi khung"))

    # ------------------------------------------------------------------ scan

    def _scan_settings(self):
        bauds = [baud for baud, var in self.scan_baud_vars.items() if var.get()]
        fmts = [fmt for fmt, var in self.scan_fmt_vars.items() if var.get()]
        low = self._read_int(self.scan_from, 1, 247, "Địa chỉ đầu")
        high = self._read_int(self.scan_to, 1, 247, "Địa chỉ cuối")
        wait = self._read_int(self.scan_wait, 20, 5000, "Thời gian chờ")
        if low > high:
            raise ValueError("Địa chỉ đầu phải ≤ địa chỉ cuối")
        return bauds, fmts, low, high, wait

    def _scan_estimate(self) -> None:
        try:
            bauds, fmts, low, high, wait = self._scan_settings()
        except ValueError:
            self.scan_estimate_var.set("")
            return
        seconds = 0.0
        for baud in bauds:
            for fmt in fmts:
                seconds += (high - low + 1) * (wait / 1000 + 15 * char_time_s(baud, fmt) + 0.01)
        total = len(bauds) * len(fmts) * (high - low + 1)
        self.scan_estimate_var.set(f"{total} lần thử, tối đa khoảng {seconds:.0f} giây" if total else "")

    def _start_scan(self) -> None:
        if not self.connected or self.scanning:
            return
        try:
            bauds, fmts, low, high, wait = self._scan_settings()
        except ValueError as exc:
            messagebox.showerror("Giá trị không hợp lệ", str(exc), parent=self.root)
            return
        if not bauds or not fmts:
            messagebox.showwarning("Chưa chọn", "Chọn ít nhất một tốc độ và một định dạng khung.", parent=self.root)
            return
        self.poll_var.set(False)
        self.scanning = True
        self.scan_cancel.clear()
        self.scan_button.state(["disabled"])
        self.scan_stop_button.state(["!disabled"])
        for widget in self.line_widgets:
            widget.state(["disabled"])
        self.scan_tree.delete(*self.scan_tree.get_children())
        self.scan_hits.clear()
        self.scan_total = len(bauds) * len(fmts) * (high - low + 1)
        self.scan_progress.configure(maximum=self.scan_total, value=0)
        client, post, stop_first = self.client, self._post, self.scan_stop.get()

        def job():
            return client.scan(bauds, fmts, range(low, high + 1), wait, self.scan_cancel, stop_first,
                               progress=lambda done, baud, fmt, address: post(
                                   self._scan_progress, (done, baud, fmt, address)),
                               found=lambda hit: post(self._scan_found, hit))

        self.run(job, self._scan_finished, self._scan_failed, status="Đang quét…")

    def _scan_progress(self, state) -> None:
        done, baud, fmt, address = state
        self.scan_progress.configure(value=done)
        self.scan_status.set(f"Đang thử {baud} bps {fmt}, địa chỉ {address}  ({done}/{self.scan_total})")

    def _scan_found(self, hit: ScanHit) -> None:
        outcome = {"ok": "TÌM THẤY", "exception": "có trả lời (mã lỗi)", "crc": "tín hiệu lỗi"}.get(hit.outcome,
                                                                                                   hit.outcome)
        iid = self.scan_tree.insert("", "end", tags=("ok",) if hit.outcome == "ok" else ("bad",), values=(
            hit.baud, hit.fmt, hit.address, outcome, fmt_word(hit.rev) if hit.rev is not None else "",
            fmt_word(hit.meter_type) if hit.meter_type is not None else "", hit.detail))
        self.scan_hits[iid] = hit

    def _scan_end(self) -> None:
        self.scanning = False
        self.scan_stop_button.state(["disabled"])
        for widget in self.line_widgets:
            widget.state(["!disabled"])
        if self.connected:
            self.scan_button.state(["!disabled"])

    def _scan_finished(self, hits: list[ScanHit]) -> None:
        self._scan_end()
        good = [hit for hit in hits if hit.outcome == "ok"]
        cancelled = self.scan_cancel.is_set()
        summary = f"Quét {'đã dừng' if cancelled else 'xong'}: tìm thấy {len(good)} đồng hồ"
        if len(hits) > len(good):
            summary += f", {len(hits) - len(good)} phản hồi lỗi"
        self.scan_status.set(summary)
        self._status(summary)
        if len(good) == 1 and not cancelled:
            hit = good[0]
            if self._confirm("Tìm thấy đồng hồ",
                             f"Đồng hồ trả lời ở {hit.baud} bps {hit.fmt}, địa chỉ {hit.address}.\n"
                             "Dùng thiết lập này để kết nối?"):
                self._apply_hit(hit)

    def _scan_failed(self, exc: Exception) -> None:
        self._scan_end()
        self.scan_status.set(f"Quét lỗi: {exc}")
        self._job_failed(exc, "Quét")

    def _use_scan_hit(self) -> None:
        selection = self.scan_tree.selection()
        hit = self.scan_hits.get(selection[0]) if selection else None
        if hit is None:
            messagebox.showinfo("Chưa chọn", "Chọn một dòng kết quả quét.", parent=self.root)
            return
        self._apply_hit(hit)

    def _apply_hit(self, hit: ScanHit) -> None:
        self.baud_var.set(str(hit.baud))
        self.fmt_var.set(hit.fmt)
        self.slave_var.set(str(hit.address))
        self._apply_line_settings()
        self._apply_client_settings()
        self._status(f"Đã chọn {hit.baud} bps {hit.fmt}, địa chỉ {hit.address}")
        if self.connected:
            self._read_parameters()
            self._read_measurements()

    # ------------------------------------------------------------------ log

    def _append_log(self, kind: str, text: str, stamp: float) -> None:
        if kind in ("tx", "rx") and not self.log_frames.get():
            return
        clock = time.strftime("%H:%M:%S", time.localtime(stamp)) + f".{int(stamp * 1000) % 1000:03d}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{clock}  {LOG_LABELS.get(kind, kind):<4} {text}\n", kind)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 4000:
            self.log_text.delete("1.0", f"{lines - 3000}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, title="Lưu nhật ký", defaultextension=".txt",
                                            filetypes=[("Text", "*.txt")],
                                            initialfile=f"ddsu666_log_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror("Không lưu được", str(exc), parent=self.root)

    # ------------------------------------------------------------------ shutdown

    def _collect_settings(self) -> dict:
        settings = dict(self.settings)
        settings["mode"] = self._mode_key()
        settings["port"] = self._selected_port()
        settings["fmt"] = self.fmt_var.get()
        settings["word_order"] = self.order_var.get()
        for key, var in (("baud", self.baud_var), ("slave", self.slave_var), ("timeout_ms", self.timeout_var),
                         ("retries", self.retries_var), ("poll_ms", self.poll_ms_var)):
            try:
                settings[key] = parse_int(var.get())
            except ValueError:
                pass
        return settings

    def _on_close(self) -> None:
        self.poll_var.set(False)
        self.scan_cancel.set()
        save_settings(self._collect_settings())
        if self.transport is not None:
            self.worker.submit(self.transport.close)
        self.worker.stop()
        self._close_csv()
        self.closing = True
        self.root.destroy()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Đọc và cài đặt đồng hồ DDSU666 qua RS485.")
    parser.add_argument("--port", help="cổng COM chọn sẵn, ví dụ COM16")
    parser.add_argument("--mode", choices=[key for key, _ in MODES],
                        help="bridge = ESP32 cầu nối, direct = USB-RS485, sim = mô phỏng")
    parser.add_argument("--connect", action="store_true", help="kết nối ngay khi mở app")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    overrides = {key: value for key, value in (("port", args.port and args.port.upper()),
                                               ("mode", args.mode)) if value}
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, overrides, args.connect)
    root.mainloop()


if __name__ == "__main__":
    main()
