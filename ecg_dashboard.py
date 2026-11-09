#!/usr/bin/env python3
"""
ECG dashboard: dark theme, live filtered ECG, big BPM, an anatomical heart
(video frames in heart_frames/) that beats in sync with every detected
R-peak, signal-quality indicator and session stats.

Needs ecg_live.py in the same folder (it reuses its filter and sources).
If the heart_frames folder is missing, a simple vector heart is used.

Usage:
    py -3.13 ecg_dashboard.py --demo
    py -3.13 ecg_dashboard.py --demo --demo-bpm 95
    py -3.13 ecg_dashboard.py                  # real ESP32 (auto port)
    py -3.13 ecg_dashboard.py --port COM5
"""
import argparse
import csv
import os
import random
import sys
import time
from collections import deque
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Polygon

from ecg_live import (FS, WINDOW_SEC, StreamFilter, detect_bpm,
                      DemoSource, SerialSource, find_port)

BG, PANEL = "#000000", "#0d1117"
GRID, ECG, RAW = "#1f6f3f", "#39ff14", "#8b949e"
RED, TXT, DIM = "#ff2d55", "#e6edf3", "#8b949e"
OK, WARN, BAD = "#3fb950", "#ffa500", "#ff5555"

# heart outline (classic parametric heart), centred on (0, 0)
_t = np.linspace(0, 2 * np.pi, 120)
HEART_X = 16 * np.sin(_t) ** 3
HEART_Y = (13 * np.cos(_t) - 5 * np.cos(2 * _t) - 2 * np.cos(3 * _t)
           - np.cos(4 * _t)) + 2.5


HEART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heart_frames")
# frame of the video where the ventricles start to contract (the R wave)
ONSET_FRAME = 17


def load_heart_frames():
    import matplotlib.image as mpimg
    if not os.path.isdir(HEART_DIR):
        return []
    files = sorted(f for f in os.listdir(HEART_DIR)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    return [mpimg.imread(os.path.join(HEART_DIR, f)) for f in files]


def heart_xy(scale):
    return np.column_stack((HEART_X * scale, HEART_Y * scale))


def signal_quality(sig):
    """Rough quality estimate: R-peak height compared with baseline noise."""
    s = sig - np.median(sig)
    noise = np.median(np.abs(s)) / 0.6745
    amp = np.percentile(np.abs(s), 99)
    snr = amp / max(noise, 1e-6)
    if snr >= 6:
        return "Good", OK
    if snr >= 3.5:
        return "Fair", WARN
    return "Poor", BAD


ART_NAMES = {"1": "Powerline 50 Hz", "2": "Baseline wander",
             "3": "Muscle EMG", "4": "Motion", "5": "Electrode pop"}


class ArtifactInjector:
    """Adds synthetic artifacts on top of the incoming signal (ADC units)."""

    def __init__(self, scale=1.0):
        self.scale = scale
        self.active = set()
        self.prev_noise = 0.0
        self.pop = 0.0
        self.next_pop = None
        self.next_motion = None
        self.motion_start = None
        self.motion_len = 1.0
        self.motion_amp = 0.0

    def toggle(self, key):
        if key in self.active:
            self.active.discard(key)
            if key == "4":
                self.next_motion, self.motion_start = None, None
            if key == "5":
                self.next_pop = None
        else:
            self.active.add(key)

    def clear(self):
        for k in list(self.active):
            self.toggle(k)

    def codes(self):
        return "+".join(sorted(self.active))

    def names(self):
        return ", ".join(ART_NAMES[k] for k in sorted(self.active))

    def sample(self, t):
        a, sc = 0.0, self.scale
        if "1" in self.active:                     # mains hum
            a += 120 * sc * np.sin(2 * np.pi * 50 * t)
        if "2" in self.active:                     # breathing + slow drift
            a += sc * (220 * np.sin(2 * np.pi * 0.25 * t)
                       + 120 * np.sin(2 * np.pi * 0.07 * t + 1.0))
        if "3" in self.active:                     # muscle noise (high freq)
            n = random.gauss(0, 1)
            a += sc * 45 * (n - self.prev_noise) / 1.414
            self.prev_noise = n
        if "4" in self.active:                     # motion bursts
            if self.next_motion is None:
                self.next_motion = t + random.uniform(1.0, 2.5)
            if self.motion_start is None and t >= self.next_motion:
                self.motion_start = t
                self.motion_len = random.uniform(0.4, 0.9)
                self.motion_amp = random.choice([-1, 1]) * random.uniform(350, 650) * sc
            if self.motion_start is not None:
                u = (t - self.motion_start) / self.motion_len
                if u >= 1:
                    self.motion_start = None
                    self.next_motion = t + random.uniform(1.5, 3.5)
                else:
                    a += self.motion_amp * (np.sin(np.pi * u) ** 2
                                            + 0.25 * np.sin(2 * np.pi * 6 * u) * np.sin(np.pi * u))
        if "5" in self.active:                     # electrode pop
            if self.next_pop is None:
                self.next_pop = t + random.uniform(1.0, 3.0)
            if t >= self.next_pop:
                self.pop = random.choice([-1, 1]) * random.uniform(500, 800) * sc
                self.next_pop = t + random.uniform(2.5, 5.0)
        a += self.pop
        self.pop *= np.exp(-1.0 / (FS * 0.35))
        return a


class Dashboard:
    def __init__(self, src, mode_label, fname, art_scale=1.0):
        self.src, self.mode, self.fname = src, mode_label, fname
        self.inj = ArtifactInjector(art_scale)
        self.bypass = False
        self.f = open(fname, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["time_s", "clean_adc", "raw_adc", "leadoff", "filtered", "artifacts"])

        n = FS * WINDOW_SEC
        self.raw, self.filt = deque(maxlen=n), deque(maxlen=n)
        self.lo = deque(maxlen=FS)
        self.flt = StreamFilter(FS)
        self.count = 0
        self.last_peak = -10 ** 9
        self.pulse_t = None
        self.last_beat_wall = 0.0
        self.last_data_wall = time.time()
        self.bpm = None
        self.bpm_hist = []
        self.last_stat = 0.0
        self.ylim = 100.0
        self.quality = ("--", DIM)
        self.frames = load_heart_frames()
        self._build()

    # ------------------------------------------------------------ layout
    def _build(self):
        self.fig = plt.figure(figsize=(13, 7.5), facecolor=BG)
        try:
            self.fig.canvas.manager.set_window_title("ECG Dashboard")
        except Exception:
            pass
        gs = GridSpec(3, 4, figure=self.fig, left=0.05, right=0.98,
                      top=0.89, bottom=0.08, wspace=0.25, hspace=0.35,
                      height_ratios=[1.5, 1.2, 0.8], width_ratios=[1, 1, 1, 1.5])

        # main filtered ECG
        self.ax = self.fig.add_subplot(gs[0:2, 0:3], facecolor=PANEL)
        self.ax.set_title("ECG (filtered)", color=TXT, loc="left", fontsize=11)
        (self.l_flt,) = self.ax.plot([], [], lw=1.6, color=ECG)
        (self.l_pk,) = self.ax.plot([], [], "o", color=RED, ms=6)
        self.ax.set_xlim(0, WINDOW_SEC)
        self.ax.set_xticks(np.arange(0, WINDOW_SEC + 0.1, 1))
        self.ax.set_xticks(np.arange(0, WINDOW_SEC + 0.1, 0.2), minor=True)
        self.ax.grid(True, which="major", color=GRID, alpha=0.6, lw=0.8)
        self.ax.grid(True, which="minor", color=GRID, alpha=0.25, lw=0.4)
        self.msg = self.ax.text(0.5, 0.5, "", transform=self.ax.transAxes,
                                ha="center", va="center", fontsize=18,
                                weight="bold", color=WARN)

        self.t_art = self.ax.text(0.01, 0.97, "", transform=self.ax.transAxes,
                                  ha="left", va="top", fontsize=11,
                                  weight="bold", color=WARN)

        # raw signal
        self.ax_raw = self.fig.add_subplot(gs[2, 0:3], facecolor=PANEL,
                                           sharex=self.ax)
        self.ax_raw.set_title("Raw ADC", color=DIM, loc="left", fontsize=9)
        (self.l_raw,) = self.ax_raw.plot([], [], lw=0.8, color=RAW)
        self.ax_raw.grid(True, color=GRID, alpha=0.3, lw=0.5)
        self.ax_raw.set_xlabel("Time (s)", color=DIM, fontsize=9)

        for a in (self.ax, self.ax_raw):
            a.tick_params(colors=DIM, labelsize=8)
            for sp in a.spines.values():
                sp.set_color("#30363d")

        # heart
        self.ax_h = self.fig.add_subplot(gs[0, 3], facecolor=BG)
        if self.frames:
            self.im = self.ax_h.imshow(self.frames[(ONSET_FRAME - 1) % len(self.frames)])
            self.ax_h.axis("off")
        else:
            self.ax_h.set_xlim(-26, 26)
            self.ax_h.set_ylim(-26, 26)
            self.ax_h.set_aspect("equal", adjustable="datalim")
            self.ax_h.axis("off")
            self.heart = Polygon(heart_xy(1.0), closed=True, fc=DIM, ec="none")
            self.ax_h.add_patch(self.heart)

        # numbers and stats
        self.ax_i = self.fig.add_subplot(gs[1:, 3], facecolor=BG)
        self.ax_i.axis("off")
        self.t_bpm = self.ax_i.text(0.5, 0.86, "--", ha="center", va="center",
                                    fontsize=54, weight="bold", color=DIM,
                                    transform=self.ax_i.transAxes)
        self.ax_i.text(0.5, 0.70, "BPM", ha="center", va="center",
                       fontsize=14, color=DIM, transform=self.ax_i.transAxes)
        self.t_stats = self.ax_i.text(0.5, 0.52, "", ha="center", va="center",
                                      fontsize=11, color=TXT, family="monospace",
                                      transform=self.ax_i.transAxes)
        self.t_qual = self.ax_i.text(0.5, 0.34, "", ha="center", va="center",
                                     fontsize=13, weight="bold", color=DIM,
                                     transform=self.ax_i.transAxes)
        self.t_info = self.ax_i.text(0.5, 0.10, "", ha="center", va="center",
                                     fontsize=9, color=DIM,
                                     transform=self.ax_i.transAxes)

        self.fig.text(0.05, 0.965, "ECG MONITOR", color=TXT, fontsize=15,
                      weight="bold", va="center")
        self.fig.text(0.05, 0.02,
                      "Artifacts:  1 Powerline   2 Baseline wander   3 Muscle EMG   "
                      "4 Motion   5 Electrode pop   0 Clear all   |   B  filter on/off (display)",
                      color=DIM, fontsize=9, va="center")
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.text(0.98, 0.965, self.mode, color=DIM, fontsize=10,
                      ha="right", va="center")

    def on_key(self, event):
        k = (event.key or "").lower()
        if k in ART_NAMES:
            self.inj.toggle(k)
        elif k == "0":
            self.inj.clear()
        elif k == "b":
            self.bypass = not self.bypass

    # ------------------------------------------------------------ update
    def update(self, _frame=None):
        now = time.time()
        new = self.src.read()
        if new:
            self.last_data_wall = now
            clean = [s[0] for s in new]
            vals = [c + self.inj.sample((self.count + i) / FS)
                    for i, c in enumerate(clean)]
            filtered = self.flt.process(vals)
            codes = self.inj.codes()
            for (c, v, fv, lo) in zip(clean, vals, filtered, [s[1] for s in new]):
                self.raw.append(v)
                self.filt.append(fv)
                self.lo.append(lo)
                self.w.writerow([f"{self.count / FS:.3f}", f"{c:.1f}", f"{v:.1f}",
                                 lo, f"{fv:.2f}", codes])
                self.count += 1
            if self.count % FS < len(new):
                self.f.flush()

        if len(self.filt) < 10:
            self.msg.set_text("Waiting for data...")
            return
        y = np.array(self.filt)
        r = np.array(self.raw)
        x = np.arange(len(y)) / FS
        yd = (r - np.median(r)) if self.bypass else y
        self.l_flt.set_data(x, yd)
        self.l_raw.set_data(x, r)
        self.ax_raw.set_ylim(r.min() - 50, r.max() + 50)
        self.ax.set_title("ECG (raw, filter bypassed)" if self.bypass else "ECG (filtered)",
                          color=BAD if self.bypass else TXT, loc="left", fontsize=11)
        view = yd[int(1.5 * FS):] if len(yd) > 2 * FS else yd
        want = max(np.abs(view).max() * 1.3, 30)
        self.ylim = want if want > self.ylim else 0.95 * self.ylim + 0.05 * want
        self.ax.set_ylim(-self.ylim, self.ylim)
        self.t_art.set_text(("Artifacts: " + self.inj.names()) if self.inj.active else "")

        no_data = (now - self.last_data_wall) > 2.0
        leads_off = sum(self.lo) > FS * 0.5
        self.msg.set_text("No data - check USB cable" if no_data else
                          "Electrodes OFF - check connections" if leads_off else "")
        self.msg.set_color(BAD if no_data else WARN)

        bpm, peaks = (None, np.array([], dtype=int))
        if not leads_off and not no_data:
            bpm, peaks = detect_bpm(y, FS)
            if bpm is not None and len(peaks) >= 4:   # irregular -> artifact, not a rhythm
                rr = np.diff(peaks) / FS
                med = np.median(rr)
                if rr.max() > 1.6 * med or rr.min() < 0.6 * med:
                    bpm = None
        if len(peaks) and bpm is not None:
            self.l_pk.set_data(peaks / FS, yd[peaks])
        else:
            self.l_pk.set_data([], [])

        # new beat -> trigger the heart
        if len(peaks) and bpm is not None:
            absolute = self.count - len(y) + peaks
            for p in absolute:
                if p > self.last_peak + int(0.3 * FS):
                    self.last_peak = p
                    age = (self.count - p) / FS
                    if age < 0.5:
                        self.pulse_t = now - age
                        self.last_beat_wall = now

        if bpm is not None:
            self.bpm = bpm
            if now - self.last_stat >= 1.0:
                self.bpm_hist.append(bpm)
                self.last_stat = now
        elif leads_off or no_data or (now - self.last_beat_wall) > 3:
            self.bpm = None

        # heart animation
        beating = self.pulse_t is not None and (now - self.last_beat_wall) < 3
        if self.frames:
            n = len(self.frames)
            if beating:
                rr = 60.0 / self.bpm if self.bpm else 0.8
                phase = min((now - self.pulse_t) / rr, 0.999)
                k = (ONSET_FRAME + int(phase * n)) % n
            else:
                k = (ONSET_FRAME - 1) % n          # relaxed heart
            self.im.set_data(self.frames[k])
            self.im.set_alpha(1.0 if beating else 0.45)
        else:
            if beating:
                scale = 1 + 0.22 * np.exp(-(now - self.pulse_t) / 0.12)
                self.heart.set_facecolor(RED)
            else:
                scale = 1.0
                self.heart.set_facecolor(DIM)
            self.heart.set_xy(heart_xy(scale))

        # texts
        if self.bpm is not None:
            self.t_bpm.set_text(f"{self.bpm:.0f}")
            self.t_bpm.set_color(RED)
        else:
            self.t_bpm.set_text("--")
            self.t_bpm.set_color(DIM)
        if self.bpm_hist:
            h = self.bpm_hist
            self.t_stats.set_text(f"avg {np.mean(h):3.0f}   min {min(h):3.0f}   max {max(h):3.0f}")
        else:
            self.t_stats.set_text("avg --   min --   max --")
        if len(y) >= FS * 4 and not leads_off and not no_data:
            self.quality = signal_quality(y)
            if bpm is None:                       # no regular rhythm found
                self.quality = ("Poor", BAD)
        elif leads_off or no_data:
            self.quality = ("--", DIM)
        self.t_qual.set_text(f"Signal: {self.quality[0]}")
        self.t_qual.set_color(self.quality[1])
        t = int(self.count / FS)
        self.t_info.set_text(f"Time {t // 60:02d}:{t % 60:02d}   Recording: {os.path.basename(self.fname)}")

    def close(self):
        try:
            self.src.close()
        finally:
            self.f.close()
            print("Saved:", self.fname)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-bpm", type=float, default=72)
    ap.add_argument("--art-scale", type=float, default=1.0,
                    help="strength of the injected artifacts (default 1)")
    args = ap.parse_args()

    if args.demo:
        src, label = DemoSource(FS, bpm=args.demo_bpm), "DEMO (simulated signal)"
    else:
        port = args.port or find_port()
        if not port:
            print("Could not find the ESP32. Run:  py -3.13 ecg_live.py --list")
            print("then:  py -3.13 ecg_dashboard.py --port COMx")
            sys.exit(1)
        src, label = SerialSource(port), f"LIVE  {port}"

    os.makedirs("recordings", exist_ok=True)
    fname = os.path.join("recordings", datetime.now().strftime("ecg_%Y%m%d_%H%M%S.csv"))
    dash = Dashboard(src, label, fname, art_scale=args.art_scale)
    anim = FuncAnimation(dash.fig, dash.update, interval=40, cache_frame_data=False)
    try:
        plt.show()
    finally:
        dash.close()


if __name__ == "__main__":
    main()
