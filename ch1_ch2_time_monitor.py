from __future__ import annotations

import csv
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class MonitorError(RuntimeError):
    pass


def finite_number(value: object) -> float:
    try:
        number = float(str(value).strip())
    except Exception as exc:
        raise MonitorError(f"示波器返回值无法解析：{value!r}") from exc
    if not math.isfinite(number) or abs(number) >= 1e20:
        raise MonitorError(f"示波器返回无效数值：{number}")
    return number


@dataclass(frozen=True)
class Sample:
    timestamp: str
    elapsed_s: float
    ch1_vavg_v: float
    ch2_vpp_v: float


class RigolScope:
    def __init__(self) -> None:
        self.manager = None
        self.instrument = None

    def connect(self) -> str:
        try:
            import pyvisa
        except ImportError as exc:
            raise MonitorError("没有安装 PyVISA，请运行“安装依赖.cmd”。") from exc

        try:
            manager = pyvisa.ResourceManager()
            resources = tuple(manager.list_resources())
        except Exception as exc:
            raise MonitorError(f"无法启动 VISA：{exc}") from exc
        if not resources:
            manager.close()
            raise MonitorError("没有发现仪器，请检查示波器 USB Device 或 LAN 连接。")

        observations: list[str] = []
        for resource in resources:
            parts = str(resource).split("::")
            if len(parts) >= 2 and parts[0].upper().startswith("USB"):
                try:
                    if int(parts[1], 0) != 0x1AB1:
                        continue
                except ValueError:
                    pass
            device = None
            try:
                device = manager.open_resource(resource)
                device.timeout = 5000
                if hasattr(device, "read_termination"):
                    device.read_termination = "\n"
                if hasattr(device, "write_termination"):
                    device.write_termination = "\n"
                identity = str(device.query("*IDN?")).strip()
                observations.append(f"{resource} -> {identity}")
                folded = identity.casefold()
                if "rigol" in folded and "ds1102z-e" in folded:
                    self.manager = manager
                    self.instrument = device
                    for channel in (1, 2):
                        if str(device.query(f":CHANnel{channel}:DISPlay?")).strip() != "1":
                            raise MonitorError(f"请先在示波器上打开 CH{channel}")
                    return identity
            except MonitorError:
                if device is not None:
                    device.close()
                manager.close()
                raise
            except Exception as exc:
                observations.append(f"{resource} -> {exc}")
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass

        manager.close()
        detail = "\n".join(observations) or "未发现可查询资源"
        raise MonitorError(f"没有找到 RIGOL DS1102Z-E：\n{detail}")

    def read_same_frame(self, stop_event: threading.Event) -> tuple[float, float] | None:
        if self.instrument is None:
            raise MonitorError("示波器尚未连接")
        scope = self.instrument
        scope.write(":RUN")
        if stop_event.wait(0.08):
            return None
        scope.write(":SINGle")
        if stop_event.wait(0.08):
            return None
        scope.write(":TFORce")

        deadline = time.monotonic() + 4.0
        while str(scope.query(":TRIGger:STATus?")).strip().upper() != "STOP":
            if stop_event.wait(0.01):
                return None
            if time.monotonic() > deadline:
                raise MonitorError("等待示波器冻结新帧超时")

        ch1 = finite_number(scope.query(":MEASure:ITEM? VAVG,CHANnel1"))
        ch2 = finite_number(scope.query(":MEASure:ITEM? VPP,CHANnel2"))
        if ch2 < 0:
            raise MonitorError(f"CH2返回负VPP：{ch2}")
        if str(scope.query(":TRIGger:STATus?")).strip().upper() != "STOP":
            raise MonitorError("读取期间示波器被重新启动，本点已丢弃")
        return ch1, ch2

    def resume(self) -> None:
        if self.instrument is not None:
            try:
                self.instrument.write(":TRIGger:SWEep AUTO")
                self.instrument.write(":RUN")
            except Exception:
                pass

    def close(self) -> None:
        self.resume()
        device, self.instrument = self.instrument, None
        manager, self.manager = self.manager, None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass


class Application:
    BG = "#0f1720"
    PANEL = "#17232f"
    TEXT = "#e8eef4"
    MUTED = "#95a7b7"
    GRID = "#2b3a47"
    CH1 = "#35d0ba"
    CH2 = "#ffad4f"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CH1 / CH2 随时间监测")
        self.root.geometry("980x720")
        self.root.minsize(760, 540)
        self.root.configure(bg=self.BG)

        self.samples: list[Sample] = []
        self.scope: RigolScope | None = None
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.started_at = 0.0

        self.status_var = tk.StringVar(value="准备就绪：CH1测VAVG，CH2测VPP")
        self.ch1_var = tk.StringVar(value="CH1  -- V")
        self.ch2_var = tk.StringVar(value="CH2  -- Vpp")
        self._build_style()
        self._build_ui()
        self.root.after(50, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Status.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("CH1.TLabel", background=self.PANEL, foreground=self.CH1, font=("Consolas", 18, "bold"))
        style.configure("CH2.TLabel", background=self.PANEL, foreground=self.CH2, font=("Consolas", 18, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 11), padding=(18, 9))
        style.configure("Accent.TButton", background=self.CH1, foreground="#071310")
        style.map("Accent.TButton", background=[("active", "#58e1cf"), ("disabled", "#416762")])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="CH1 / CH2 随时间变化", style="Title.TLabel").pack(side="left")
        buttons = ttk.Frame(header)
        buttons.pack(side="right")
        self.start_button = ttk.Button(buttons, text="开始", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(buttons, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="清空", command=self._clear).pack(side="left", padx=4)
        ttk.Button(buttons, text="导出", command=self._export).pack(side="left", padx=4)

        info = ttk.Frame(outer, style="Panel.TFrame", padding=12)
        info.pack(fill="x", pady=(12, 10))
        ttk.Label(info, textvariable=self.ch1_var, style="CH1.TLabel").pack(side="left", padx=(0, 35))
        ttk.Label(info, textvariable=self.ch2_var, style="CH2.TLabel").pack(side="left")
        ttk.Label(info, textvariable=self.status_var, style="Status.TLabel").pack(side="right")

        self.canvas = tk.Canvas(outer, bg="#0a1219", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._draw())

        ttk.Label(outer, text="反复遮光/放光即可；上图是CH1平均电压，下图是CH2峰峰值。每一点来自同一个冻结帧。", foreground=self.MUTED).pack(anchor="w", pady=(8, 0))

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.started_at = time.monotonic()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("正在连接示波器…")

        def task() -> None:
            try:
                if self.scope is None:
                    scope = RigolScope()
                    identity = scope.connect()
                    self.scope = scope
                    self.events.put(("status", f"已连接：{identity.split(',')[1] if ',' in identity else identity}"))
                while not self.stop_event.is_set():
                    assert self.scope is not None
                    values = self.scope.read_same_frame(self.stop_event)
                    if values is None:
                        break
                    ch1, ch2 = values
                    sample = Sample(
                        timestamp=datetime.now().isoformat(timespec="milliseconds"),
                        elapsed_s=time.monotonic() - self.started_at,
                        ch1_vavg_v=ch1,
                        ch2_vpp_v=ch2,
                    )
                    self.events.put(("sample", sample))
                    if self.stop_event.wait(0.12):
                        break
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                if self.scope is not None:
                    self.scope.resume()
                self.events.put(("stopped", None))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止…")

    def _clear(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在监测", "请先停止，再清空。", parent=self.root)
            return
        self.samples.clear()
        self.ch1_var.set("CH1  -- V")
        self.ch2_var.set("CH2  -- Vpp")
        self.status_var.set("数据已清空")
        self._draw()

    def _export(self) -> None:
        if not self.samples:
            messagebox.showinfo("没有数据", "先开始监测。", parent=self.root)
            return
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出时间曲线",
            defaultextension=".csv",
            initialfile=f"CH1_CH2_time_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not filename:
            return
        with open(filename, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "elapsed_s", "CH1_VAVG_V", "CH2_VPP_V"])
            for s in self.samples:
                writer.writerow([s.timestamp, f"{s.elapsed_s:.6f}", f"{s.ch1_vavg_v:.9g}", f"{s.ch2_vpp_v:.9g}"])
        self.status_var.set(f"已导出 {len(self.samples)} 点")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "sample":
                    sample = payload
                    assert isinstance(sample, Sample)
                    self.samples.append(sample)
                    self.ch1_var.set(f"CH1  {sample.ch1_vavg_v:+.5f} V")
                    self.ch2_var.set(f"CH2  {sample.ch2_vpp_v:.5f} Vpp")
                    self.status_var.set(f"监测中 · {len(self.samples)} 点")
                    self._draw()
                elif kind == "error":
                    self.status_var.set(f"错误：{payload}")
                    messagebox.showerror("监测错误", str(payload), parent=self.root)
                elif kind == "stopped":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if not self.status_var.get().startswith("错误"):
                        self.status_var.set(f"已停止 · {len(self.samples)} 点")
        except queue.Empty:
            pass
        self.root.after(50, self._poll_events)

    @staticmethod
    def _range(values: list[float]) -> tuple[float, float]:
        lo, hi = min(values), max(values)
        span = hi - lo
        if span < 1e-9:
            margin = max(abs(lo) * 0.05, 0.005)
        else:
            margin = span * 0.15
        return lo - margin, hi + margin

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        w = max(canvas.winfo_width(), 400)
        h = max(canvas.winfo_height(), 360)
        if len(self.samples) < 2:
            canvas.create_text(w / 2, h / 2, text="点击“开始”，然后反复遮光/放光", fill=self.MUTED, font=("Microsoft YaHei UI", 14))
            return

        visible = self.samples[-300:]
        ts = [s.elapsed_s for s in visible]
        x0, x1 = ts[0], ts[-1]
        if x1 - x0 < 1e-9:
            x1 = x0 + 1.0
        pad_l, pad_r, pad_t, pad_b = 78, 22, 28, 42
        gap = 42
        panel_h = (h - pad_t - pad_b - gap) / 2

        def sx(value: float) -> float:
            return pad_l + (value - x0) / (x1 - x0) * (w - pad_l - pad_r)

        def panel(values: list[float], top: float, color: str, title: str, unit: str) -> None:
            y0, y1 = self._range(values)

            def sy(value: float) -> float:
                return top + panel_h - (value - y0) / (y1 - y0) * panel_h

            for i in range(5):
                gx = x0 + (x1 - x0) * i / 4
                gy = y0 + (y1 - y0) * i / 4
                canvas.create_line(sx(gx), top, sx(gx), top + panel_h, fill=self.GRID)
                canvas.create_line(pad_l, sy(gy), w - pad_r, sy(gy), fill=self.GRID)
                canvas.create_text(pad_l - 8, sy(gy), text=f"{gy:.4g}", anchor="e", fill=self.MUTED, font=("Consolas", 9))
            points: list[float] = []
            for t, value in zip(ts, values):
                points.extend((sx(t), sy(value)))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2.5, smooth=False)
            canvas.create_text(pad_l, top - 8, text=f"{title} / {unit}", anchor="sw", fill=color, font=("Microsoft YaHei UI", 11, "bold"))

        panel([s.ch1_vavg_v for s in visible], pad_t, self.CH1, "CH1 VAVG", "V")
        panel([s.ch2_vpp_v for s in visible], pad_t + panel_h + gap, self.CH2, "CH2 VPP", "Vpp")
        for i in range(5):
            gx = x0 + (x1 - x0) * i / 4
            canvas.create_text(sx(gx), h - 20, text=f"{gx:.1f}", fill=self.MUTED, font=("Consolas", 9))
        canvas.create_text(w / 2, h - 5, text="时间 / s（显示最近300点）", fill=self.MUTED)

    def _close(self) -> None:
        self.stop_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.5)
        if self.scope is not None:
            self.scope.close()
        self.root.destroy()


def self_test() -> int:
    assert finite_number("0.123") == 0.123
    try:
        finite_number("9.9e37")
    except MonitorError:
        pass
    else:
        raise AssertionError("RIGOL invalid sentinel was not rejected")
    lo, hi = Application._range([1.0, 1.0])
    assert lo < 1.0 < hi
    lo, hi = Application._range([1.0, 2.0, 3.0])
    assert lo < 1.0 and hi > 3.0
    print("SELF_TEST_PASS")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    root = tk.Tk()
    Application(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
