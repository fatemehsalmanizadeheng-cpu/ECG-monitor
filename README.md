# ESP32 ECG Monitor

A low-cost ECG monitor: an **AD8232** ECG front-end read by an **ESP32 (WROOM32)**, streamed over USB to a Python dashboard that filters the signal, shows it live, estimates heart rate (BPM) and records it to CSV.

University project (biomedical engineering). Educational use only.

> **Not a medical device.** Do not use it for diagnosis or monitoring of any health condition.

## Features
- Live filtered ECG (0.5-40 Hz band-pass + 50 Hz notch) with raw signal below
- R-peak detection and BPM, with session min / avg / max
- Signal-quality indicator and lead-off detection
- Animated heart that beats in sync with each detected R-peak
- **Artifact lab:** inject powerline hum, baseline wander, muscle noise (EMG), motion and electrode-pop artifacts with the keyboard, and compare filtered vs. unfiltered display
- Automatic CSV recording (clean signal, signal with artifacts, filtered signal, artifact flags)
- Demo mode with a simulated ECG, so the software can be tested without hardware

## Hardware
| Part | Notes |
|------|-------|
| ESP32 WROOM32 DevKit | with USB-UART chip (CP2102 / CH340) |
| AD8232 ECG module | with 3-lead cable and disposable electrode pads |
| Jumper wires, breadboard, USB data cable | |

### Wiring
| AD8232 | ESP32 |
|--------|-------|
| 3.3V | 3V3 |
| GND | GND |
| OUTPUT | GPIO34 |
| LO+ | GPIO32 |
| LO- | GPIO33 |
| SDN | not connected |

Electrodes: **RA** right chest below the collarbone, **LA** left chest below the collarbone, **RL** lower right abdomen (reference).

**Safety:** while electrodes are on the body, run the laptop on battery (unplug the charger).

## Quick start
1. Install Python 3.10+ and the libraries:
   ```
   pip install -r requirements.txt
   ```
2. Try the dashboard without hardware:
   ```
   python ecg_dashboard.py --demo
   ```
   (On Windows with several Python versions use `py -3.13 ...`.)
3. Flash `arduino/ecg_esp32/ecg_esp32.ino` with the Arduino IDE (install "esp32 by Espressif Systems", board: *ESP32 Dev Module*). Close the Serial Monitor afterwards.
4. Run with the real board:
   ```
   python ecg_dashboard.py
   ```
   or choose the port yourself: `python ecg_dashboard.py --port COM5`

A simpler viewer is also included: `python ecg_live.py [--demo] [--port COMx] [--list]`.

## Artifact keys (dashboard window)
| Key | Artifact |
|-----|----------|
| 1 | Powerline 50 Hz |
| 2 | Baseline wander |
| 3 | Muscle EMG |
| 4 | Motion |
| 5 | Electrode pop |
| 0 | Clear all |
| B | Filter on/off (display only) |

The filter removes powerline hum, baseline wander and most muscle noise. Motion and electrode-pop artifacts overlap the ECG band and cannot be filtered out; the dashboard then shows no BPM and a *Poor* signal quality.

Use `--art-scale 2` to make injected artifacts stronger.

## Project structure
```
arduino/ecg_esp32/ecg_esp32.ino   ESP32 firmware (250 samples/s over serial)
ecg_live.py                       filter, BPM detection, data sources, simple viewer
ecg_dashboard.py                  dashboard (animated heart, artifacts, quality)
requirements.txt
```

## How it works
The ESP32 samples the AD8232 output at 250 Hz and prints `value,leadoff` lines at 115200 baud. The Python side filters the stream sample by sample, detects R-peaks on a 6-second window and estimates BPM from the median RR interval.

## Limitations
- Tested with a simulated signal and the hardware described above; no clinical validation.
- BPM detection needs about 4 seconds of clean signal and is not an arrhythmia detector.

## Credits
Architecture (USB serial + Python processing) inspired by [aaphraim/Pocket-ECG-Monitor](https://github.com/aaphraim/Pocket-ECG-Monitor).

---

## خلاصه به فارسی
مانیتور ECG ارزان با ESP32 و AD8232. سیگنال از طریق USB به یک داشبورد پایتون می‌رسد که فیلتر می‌کند، نمایش زنده می‌دهد، BPM را حساب می‌کند و در فایل CSV ذخیره می‌کند. حالت `--demo` بدون سخت‌افزار کار می‌کند. این پروژه آموزشی است و **دستگاه پزشکی نیست**.
