#!/usr/bin/env python3
"""
Live ECG viewer: reads ESP32 + AD8232 over USB serial, filters the signal,
shows it live, estimates heart rate (BPM) and records everything to CSV.

Usage:
    python ecg_live.py --demo            # no hardware, simulated ECG
    python ecg_live.py                   # auto-detect the ESP32 port
    python ecg_live.py --port COM5       # choose the port yourself
    python ecg_live.py --list            # list serial ports
"""
import argparse
import csv
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

FS = 250            # must match FS in the Arduino sketch
WINDOW_SEC = 6      # seconds shown on screen


# ---------------------------------------------------------------- filtering
# Pre-computed filter for FS = 250 Hz (no scipy needed):
#   low-pass 40 Hz, high-pass 0.5 Hz, 50 Hz notch (mains).
# Each row: b0, b1, b2, a0, a1, a2
SOS = [
    [0.1424442454075173, 0.2848884908150346, 0.1424442454075173, 1.0, -0.6856000535320614, 0.26069603227349614],
    [1.0, -2.0, 1.0, 1.0, -1.9822347503854438, 0.9823948462782603],
    [0.9794827609814495, -0.6053536376811252, 0.9794827609814492, 1.0, -0.6053536376811253, 0.958965521962899],
]


class StreamFilter:
    """Filters samples one by one as they arrive (keeps its state)."""

    def __init__(self, fs):
        if fs != 250:
            raise ValueError("The built-in filter is designed for FS = 250 Hz")
        self.z = [[0.0, 0.0] for _ in SOS]
        self.x0 = None

    def process(self, x):
        out = []
        for v in x:
            if self.x0 is None:           # remove the DC level at start-up
                self.x0 = float(v)
            y = float(v) - self.x0
            for i, (b0, b1, b2, _a0, a1, a2) in enumerate(SOS):
                z = self.z[i]
                o = b0 * y + z[0]
                z[0] = b1 * y - a1 * o + z[1]
                z[1] = b2 * y - a2 * o
                y = o
            out.append(y)
        return np.array(out)


# ---------------------------------------------------------------- heart rate
def find_peaks_simple(s, height, distance):
    """Local maxima above `height`, at least `distance` samples apart."""
    idx = np.where((s[1:-1] > s[:-2]) & (s[1:-1] >= s[2:]))[0] + 1
    idx = idx[s[idx] >= height]
    order = idx[np.argsort(-s[idx])]       # tallest first
    kept = []
    for i in order:
        if all(abs(i - k) >= distance for k in kept):
            kept.append(i)
    return np.array(sorted(kept), dtype=int)


def detect_bpm(sig, fs):
    """Return (bpm or None, peak indices) from a filtered signal window."""
    empty = np.array([], dtype=int)
    if len(sig) < fs * 4:
        return None, empty
    s = sig - np.median(sig)
    hi = np.percentile(s, 99)
    lo = np.percentile(s, 1)
    if abs(lo) > abs(hi):          # electrodes swapped -> inverted ECG
        s = -s
        hi = -lo
    if hi <= 0:
        return None, empty
    peaks = find_peaks_simple(s, 0.5 * hi, int(0.3 * fs))
    if len(peaks) < 3:
        return None, peaks
    rr = np.diff(peaks) / fs
    bpm = 60.0 / np.median(rr)
    if not 40 <= bpm <= 180:
        return None, peaks
    return bpm, peaks


# ---------------------------------------------------------------- sources
class DemoSource:
    """Simulated ECG (about 72 bpm) with noise and 50 Hz hum."""

    def __init__(self, fs, bpm=72):
        self.fs, self.bpm = fs, bpm
        self.t0 = time.time()
        self.n = 0

    def _sample(self, t):
        period = 60.0 / self.bpm
        ph = (t % period) / period

        def g(c, w, a):
            return a * np.exp(-((ph - c) ** 2) / (2 * w * w))

        ecg = (g(0.18, 0.025, 0.12) + g(0.30, 0.008, -0.15) + g(0.32, 0.010, 1.0)
               + g(0.34, 0.010, -0.25) + g(0.55, 0.04, 0.3))
        v = (2000 + 400 * ecg + 15 * np.random.randn()
             + 30 * np.sin(2 * np.pi * 50 * t) + 80 * np.sin(2 * np.pi * 0.2 * t))
        return v, 0

    def read(self):
        target = int((time.time() - self.t0) * self.fs)
        out = []
        while self.n < target:
            out.append(self._sample(self.n / self.fs))
            self.n += 1
        return out

    def close(self):
        pass


class SerialSource:
    def __init__(self, port, baud=115200):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.q = queue.Queue()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
            except Exception:
                time.sleep(0.1)
                continue
            if not line:
                continue
            parts = line.split(",")
            try:
                v = int(parts[0])
                lo = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                continue                      # boot messages / garbage lines
            self.q.put((v, lo))

    def read(self):
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self.running = False
        try:
            self.ser.close()
        except Exception:
            pass


def list_ports():
    from serial.tools import list_ports as lp
    return list(lp.comports())


def find_port():
    ports = list_ports()
    keys = ("cp210", "ch340", "ch910", "usb", "uart", "silicon labs")
    for p in ports:
        if any(k in (p.description or "").lower() for k in keys):
            return p.device
    return ports[0].device if len(ports) == 1 else None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for p in list_ports():
            print(p.device, "-", p.description)
        return

    if args.demo:
        src = DemoSource(FS)
        print("Demo mode (simulated ECG).")
    else:
        port = args.port or find_port()
        if not port:
            print("Could not find the ESP32. Run:  python ecg_live.py --list")
            print("then:  python ecg_live.py --port COMx")
            sys.exit(1)
        print("Opening", port)
        src = SerialSource(port)

    os.makedirs("recordings", exist_ok=True)
    fname = os.path.join("recordings", datetime.now().strftime("ecg_%Y%m%d_%H%M%S.csv"))
    f = open(fname, "w", newline="")
    w = csv.writer(f)
    w.writerow(["time_s", "raw_adc", "leadoff", "filtered"])
    print("Recording to", fname)

    n = FS * WINDOW_SEC
    raw_buf, filt_buf = deque(maxlen=n), deque(maxlen=n)
    lo_buf = deque(maxlen=FS)                 # last second of lead-off flags
    flt = StreamFilter(FS)
    state = {"count": 0, "bpm": None, "t0": None}

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [1, 2]})
    fig.suptitle("ECG - ESP32 + AD8232")
    (l_raw,) = ax1.plot([], [], lw=0.8, color="gray")
    (l_flt,) = ax2.plot([], [], lw=1.2, color="tab:red")
    (l_pk,) = ax2.plot([], [], "o", color="tab:blue", ms=6)
    ax1.set_ylabel("Raw (ADC)")
    ax2.set_ylabel("Filtered")
    ax2.set_xlabel("Time (s)")
    ax2.set_xlim(0, WINDOW_SEC)
    ax1.grid(alpha=0.3)
    ax2.grid(alpha=0.3)
    txt = ax2.text(0.01, 0.95, "", transform=ax2.transAxes, va="top",
                   fontsize=16, weight="bold")

    def update(_):
        new = src.read()
        if new:
            vals = [s[0] for s in new]
            los = [s[1] for s in new]
            if state["t0"] is None:
                state["t0"] = time.time()
            filtered = flt.process(vals)
            for v, lo, fv in zip(vals, los, filtered):
                raw_buf.append(v)
                filt_buf.append(fv)
                lo_buf.append(lo)
                w.writerow([f"{state['count'] / FS:.3f}", v, lo, f"{fv:.2f}"])
                state["count"] += 1
            if state["count"] % FS < len(new):
                f.flush()

        if len(filt_buf) < 10:
            return
        y_raw = np.array(raw_buf)
        y_flt = np.array(filt_buf)
        x = np.arange(len(y_flt)) / FS
        l_raw.set_data(x, y_raw)
        l_flt.set_data(x, y_flt)
        ax1.set_ylim(y_raw.min() - 50, y_raw.max() + 50)
        # skip the first 1.5 s while the filter settles
        view = y_flt[int(1.5 * FS):] if len(y_flt) > 2 * FS else y_flt
        lim = max(np.abs(view).max() * 1.2, 20)
        ax2.set_ylim(-lim, lim)

        if sum(lo_buf) > FS * 0.5:
            txt.set_text("Electrodes OFF - check connections")
            txt.set_color("tab:orange")
            l_pk.set_data([], [])
            return
        bpm, peaks = detect_bpm(y_flt, FS)
        if len(peaks):
            l_pk.set_data(peaks / FS, y_flt[peaks])
        else:
            l_pk.set_data([], [])
        if bpm is not None:
            state["bpm"] = bpm
            txt.set_text(f"HR: {bpm:.0f} BPM")
            txt.set_color("tab:green")
        else:
            txt.set_text("HR: --  (waiting for a clean signal)")
            txt.set_color("black")

    anim = FuncAnimation(fig, update, interval=40, cache_frame_data=False)
    try:
        plt.show()
    finally:
        src.close()
        f.close()
        print("Saved:", fname)


if __name__ == "__main__":
    main()
