"""Soundview - audio-reactive display for a Turing Smart Screen "Rev A" 3.5" LCD on a serial port (COM3).

Captures whatever the PC is playing (WASAPI loopback) and draws either
  * a digital spectrum analyser (LED-segment bars with peak hold), or
  * an analog L/R needle meter.

Keys (console):  m / space = toggle   s = spectrum   l = meter   q = quit
Usage:  python soundview.py [--port COM3] [--mode spectrum|meter] [--cycle SEC] [--no-rotate] [--preview out.png]
"""
import argparse
import math
import msvcrt
import sys
import threading
import time

import numpy as np
import serial
from PIL import Image, ImageDraw, ImageFont
from serial.tools import list_ports

W, H = 480, 320
BRIGHTNESS = 60          # 0-100
FPS = 30                 # target; real rate is limited by the serial link, only changed tiles are sent
TILE = 16
FONTS = r"C:\Windows\Fonts"

BG = (11, 13, 18)
DIM = (140, 148, 162)
TEXT = (232, 236, 242)
GRID = (52, 58, 72)
OFF = (26, 31, 41)
GREEN, AMBER, RED = (0, 200, 90), (255, 170, 0), (255, 84, 84)


def font(name, size):
    try:
        return ImageFont.truetype(f"{FONTS}\\{name}", size)
    except OSError:
        return ImageFont.load_default()


F_LBL = font("consolab.ttf", 13)
F_SM = font("consola.ttf", 11)
F_BIG = font("segoeuib.ttf", 26)


# ----------------------------------------------------------------------------- audio
class Audio:
    """WASAPI loopback of the default output device -> latest mono/stereo samples."""

    N = 2048

    def __init__(self):
        import pyaudiowpatch as pa
        self.pa = pa.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pa.paWASAPI)
        dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev.get("isLoopbackDevice"):
            for lb in self.pa.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
        self.name = dev["name"]
        self.rate = int(dev["defaultSampleRate"])
        self.ch = max(1, int(dev["maxInputChannels"]))
        self.buf = np.zeros((self.N, 2), np.float32)
        self.last = 0.0
        self.lock = threading.Lock()
        self.stream = self.pa.open(format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
                                   frames_per_buffer=512, input_device_index=dev["index"],
                                   stream_callback=self._cb)

    def _cb(self, data, frames, t, status):
        a = np.frombuffer(data, np.int16).reshape(-1, self.ch).astype(np.float32) / 32768.0
        st = a[:, :2] if self.ch >= 2 else np.repeat(a[:, :1], 2, axis=1)
        with self.lock:
            n = len(st)
            self.buf = np.concatenate((self.buf[n:], st))[-self.N:]
            self.last = time.time()
        import pyaudiowpatch as pa
        return (None, pa.paContinue)

    def snapshot(self):
        with self.lock:
            if time.time() - self.last > 0.15:      # loopback sends nothing while silent
                return np.zeros((self.N, 2), np.float32)
            return self.buf.copy()

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
            self.pa.terminate()
        except Exception:
            pass


# ----------------------------------------------------------------------------- LCD
class Lcd:
    """Turing Smart Screen Rev A (USB35INCHIPSV2); sends only the tiles that changed."""

    SCREEN_ON, SET_BRIGHTNESS, SET_ORIENTATION, DISPLAY_BITMAP = 109, 110, 121, 197
    LANDSCAPE = 2

    def __init__(self, port=None, rotate=True):
        self.port = port or self.find_port()
        if not self.port:
            raise serial.SerialException("LCD not found")
        self.rotate = rotate
        self.ser = serial.Serial(self.port, 115200, timeout=1, write_timeout=5, rtscts=True)
        self.cmd(self.SCREEN_ON)
        self.cmd(self.SET_BRIGHTNESS, int(255 - BRIGHTNESS / 100 * 255))
        buf = bytearray(16)
        buf[0:6] = self.pack(self.SET_ORIENTATION, 0, 0, 0, 0)
        buf[6] = self.LANDSCAPE + 100
        buf[7], buf[8], buf[9], buf[10] = W >> 8, W & 255, H >> 8, H & 255
        self.ser.write(bytes(buf))
        self.prev = None
        self.sent = 0
        self.grid = self.uniform_grid()

    @staticmethod
    def uniform_grid():
        return list(range(0, W, TILE)) + [W], list(range(0, H, TILE)) + [H]

    @staticmethod
    def find_port():
        for p in list_ports.comports():
            if p.serial_number == "USB35INCHIPSV2" or (p.vid == 0x1A86 and p.pid == 0x5722):
                return p.device
        return None

    @staticmethod
    def pack(cmd, x, y, ex, ey):
        return bytes([x >> 2, ((x & 3) << 6) + (y >> 4), ((y & 15) << 4) + (ex >> 6),
                      ((ex & 63) << 2) + (ey >> 8), ey & 255, cmd])

    def cmd(self, c, x=0, y=0, ex=0, ey=0):
        self.ser.write(self.pack(c, x, y, ex, ey))

    def show(self, img):
        a = np.asarray(img, dtype=np.uint16)
        if self.rotate:
            a = a[::-1, ::-1]
        p = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        if self.prev is None:
            self._rect(p, 0, 0, W, H)
        else:
            xs, ys = self.grid
            if self.rotate:
                xs, ys = [W - x for x in xs[::-1]], [H - y for y in ys[::-1]]
            diff = (p != self.prev)
            col = np.add.reduceat(diff.astype(np.uint8), xs[:-1], axis=1)
            dirty = np.add.reduceat(col, ys[:-1], axis=0) > 0
            for c in range(len(xs) - 1):
                r = 0
                while r < len(ys) - 1:
                    if not dirty[r, c]:
                        r += 1
                        continue
                    r0 = r
                    while r < len(ys) - 1 and dirty[r, c]:
                        r += 1
                    self._rect(p, xs[c], ys[r0], xs[c + 1], ys[r])
        self.prev = p

    def _rect(self, p, x0, y0, x1, y1):
        data = np.ascontiguousarray(p[y0:y1, x0:x1]).tobytes()
        # header and pixel data must be separate writes (one combined write corrupts the panel)
        self.ser.write(self.pack(self.DISPLAY_BITMAP, x0, y0, x1 - 1, y1 - 1))
        self.ser.write(data)
        self.sent += len(data)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------- spectrum
class Spectrum:
    BANDS, SEGS = 32, 26
    GRID = (sorted({0, W} | {16 + 14 * k + o for k in range(32) for o in (0, 11)}),
            sorted({0, H} | {40 + 10 * k + o for k in range(26) for o in (0, 8)}))
    DB_MIN, DB_MAX = -70.0, -10.0

    def __init__(self, rate):
        self.rate = rate
        n = Audio.N
        self.win = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, 1 / rate)
        edges = np.geomspace(40, min(16000, rate / 2 - 1), self.BANDS + 1)
        self.idx = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(self.BANDS)]
        for i, ix in enumerate(self.idx):
            if len(ix) == 0:
                self.idx[i] = np.array([int(np.argmin(np.abs(freqs - edges[i])))])
        self.edges = edges
        self.lvl = np.zeros(self.BANDS)
        self.lit = np.zeros(self.BANDS, int)
        self.pk = np.zeros(self.BANDS, int)
        self.peak = np.zeros(self.BANDS)
        self.peak_t = np.zeros(self.BANDS)
        self.t = time.time()
        # +3 dB/oct-ish tilt so highs are visible
        self.tilt = np.array([3.0 * math.log2(math.sqrt(edges[i] * edges[i + 1]) / 1000) for i in range(self.BANDS)])
        self.bg = self._background()

    def _background(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((12, 8), "SPECTRUM", font=F_LBL, fill=DIM)
        for f, lab in ((100, "100"), (1000, "1k"), (10000, "10k")):
            i = np.searchsorted(self.edges, f) - 1
            x = 16 + i * 14 + 5
            d.text((x - 8, H - 17), lab, font=F_SM, fill=DIM)
        return img

    def update(self, st):
        mono = st.mean(axis=1)
        spec = np.abs(np.fft.rfft(mono * self.win)) / (Audio.N / 4)
        db = np.array([20 * np.log10(spec[ix].max() + 1e-9) for ix in self.idx]) + self.tilt
        tgt = np.clip((db - self.DB_MIN) / (self.DB_MAX - self.DB_MIN), 0, 1)
        now = time.time()
        dt = now - self.t
        self.t = now
        up = tgt > self.lvl
        self.lvl = np.where(up, self.lvl + (tgt - self.lvl) * min(1, dt * 30), self.lvl - dt * 1.4)
        self.lvl = np.clip(self.lvl, 0, 1)
        hold = self.lvl >= self.peak
        self.peak_t = np.where(hold, now, self.peak_t)
        self.peak = np.where(hold, self.lvl, np.where(now - self.peak_t > 0.6, self.peak - dt * 0.8, self.peak))
        self.peak = np.clip(self.peak, 0, 1)
        want = self.lvl * self.SEGS
        self.lit = np.where(np.abs(want - self.lit) > 0.7, np.round(want), self.lit).astype(int)
        self.pk = np.minimum(self.SEGS - 1, (self.peak * self.SEGS).astype(int))

    def render(self):
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        top, bot = 40, 300
        pitch = (bot - top) / self.SEGS
        for b in range(self.BANDS):
            x = 16 + b * 14
            lit, pk = self.lit[b], self.pk[b]
            for s in range(self.SEGS):
                y1 = bot - s * pitch
                y0 = y1 - pitch + 2
                frac = s / (self.SEGS - 1)
                col = GREEN if frac < 0.62 else AMBER if frac < 0.85 else RED
                if s < lit:
                    d.rectangle((x, y0, x + 10, y1 - 1), fill=col)
                elif s == pk and self.peak[b] > 0.04:
                    d.rectangle((x, y0, x + 10, y1 - 1), fill=TEXT)
                else:
                    d.rectangle((x, y0, x + 10, y1 - 1), fill=OFF)
        return img


# ----------------------------------------------------------------------------- analog meter
class Meter:
    DB_MIN, DB_MAX = -40.0, 3.0
    A0, A1 = 234.0, 306.0          # needle sweep in screen degrees (clockwise from the left), see _pt
    PIV_Y, R = 335, 205            # pivot is below the panel so only the arc's top is visible

    def __init__(self):
        self.v = [0.0, 0.0]
        self.pk = [0.0, 0.0]
        self.pk_t = [0.0, 0.0]
        self.t = time.time()
        self.bgs = [self._background(0), self._background(1)]
        self.bg = Image.new("RGB", (W, H), BG)
        self.bg.paste(self.bgs[0], (0, 0))
        self.bg.paste(self.bgs[1], (W // 2, 0))

    def _ang(self, db):
        f = (min(max(db, self.DB_MIN), self.DB_MAX) - self.DB_MIN) / (self.DB_MAX - self.DB_MIN)
        return self.A0 + f * (self.A1 - self.A0)

    def _pt(self, cx, ang, r):
        a = math.radians(ang)
        return (cx + r * math.cos(a), self.PIV_Y + r * math.sin(a))

    def _background(self, ch):
        w = W // 2
        img = Image.new("RGB", (w, H), (20, 24, 33))
        d = ImageDraw.Draw(img)
        d.rectangle((3, 3, w - 4, H - 4), outline=(38, 44, 58))
        cx = w / 2
        zero = self._ang(0)
        # colour arc
        for a in np.arange(self.A0, self.A1, 0.5):
            col = RED if a >= zero else (200, 205, 215)
            p0, p1 = self._pt(cx, a, self.R), self._pt(cx, a + 0.7, self.R)
            d.line((p0, p1), fill=col, width=3)
        for db in range(-40, 4):
            major = db in (-40, -30, -20, -10, -7, -5, -3, 0, 3)
            if not major and db % 2:
                continue
            a = self._ang(db)
            col = RED if db > 0 else TEXT if major else DIM
            d.line((self._pt(cx, a, self.R), self._pt(cx, a, self.R - (14 if major else 7))), fill=col, width=2 if major else 1)
            if db in (-40, -30, -20, -10, 0, 3):
                lab = f"{db:+d}" if db > 0 else str(db)
                tx, ty = self._pt(cx, a, self.R - 30)
                bb = d.textbbox((0, 0), lab, font=F_SM)
                d.text((tx - (bb[2] - bb[0]) / 2, ty - (bb[3] - bb[1]) / 2), lab, font=F_SM, fill=col)
        d.text((cx - 10, 196), "dB", font=F_LBL, fill=DIM)
        d.text((cx - 12, 255), "LR"[ch], font=F_BIG, fill=TEXT)
        return img

    def update(self, st):
        now = time.time()
        dt = now - self.t
        self.t = now
        n = int(Audio.N * 0.4)
        rms = np.sqrt((st[-n:] ** 2).mean(axis=0))
        pk = np.abs(st[-n:]).max(axis=0)
        for c in range(2):
            db = 20 * math.log10(rms[c] + 1e-9)
            tgt = min(max(db, self.DB_MIN), self.DB_MAX)
            k = min(1, dt * (14 if tgt > self.v[c] else 4))
            self.v[c] += (tgt - self.v[c]) * k
            pdb = 20 * math.log10(pk[c] + 1e-9)
            if pdb >= self.pk[c]:
                self.pk[c], self.pk_t[c] = min(pdb, self.DB_MAX), now
            elif now - self.pk_t[c] > 1.0:
                self.pk[c] = max(self.DB_MIN, self.pk[c] - dt * 20)

    def render(self):
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        for c in range(2):
            cx = c * (W // 2) + (W // 4)
            pa = self._ang(self.pk[c])
            d.line((self._pt(cx, pa, self.R - 3), self._pt(cx, pa, self.R + 8)), fill=AMBER, width=3)
            a = self._ang(self.v[c])
            d.line((self._pt(cx + 2, a, 40), self._pt(cx + 2, a, self.R - 6)), fill=(8, 9, 12), width=4)   # shadow
            d.line((self._pt(cx, a, 40), self._pt(cx, a, self.R - 6)), fill=(255, 90, 70), width=3)
            d.ellipse((cx - 16, self.PIV_Y - 16, cx + 16, self.PIV_Y + 16), fill=(50, 56, 70), outline=(90, 98, 114))
            d.text((cx + 50, 290), f"{self.v[c]:+5.1f}", font=F_SM, fill=DIM)
        return img


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--mode", choices=("spectrum", "meter"), default="spectrum")
    ap.add_argument("--cycle", type=float, default=0, help="auto-switch every SEC seconds")
    ap.add_argument("--no-rotate", action="store_true", help="screen is not mounted upside down")
    ap.add_argument("--preview", help="write one frame of each mode to PNG files and exit (no LCD)")
    a = ap.parse_args()

    audio = Audio()
    print(f"audio: {audio.name} {audio.rate}Hz {audio.ch}ch")
    modes = {"spectrum": Spectrum(audio.rate), "meter": Meter()}

    if a.preview:
        time.sleep(1.0)
        for k, m in modes.items():
            for _ in range(20):
                m.update(audio.snapshot())
                time.sleep(0.03)
            m.render().save(f"{a.preview}_{k}.png")
        audio.close()
        return

    lcd = Lcd(a.port, rotate=not a.no_rotate)
    print(f"lcd: {lcd.port}   keys: m/space=toggle s=spectrum l=meter q=quit")
    mode, t_mode, frames, t_fps = a.mode, time.time(), 0, time.time()
    try:
        while True:
            t0 = time.time()
            while msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch in ("m", " "):
                    mode = "meter" if mode == "spectrum" else "spectrum"
                    lcd.prev = None
                elif ch == "s" and mode != "spectrum":
                    mode, lcd.prev = "spectrum", None
                elif ch == "l" and mode != "meter":
                    mode, lcd.prev = "meter", None
                elif ch == "q":
                    return
            if a.cycle and time.time() - t_mode > a.cycle:
                mode = "meter" if mode == "spectrum" else "spectrum"
                lcd.prev, t_mode = None, time.time()
            m = modes[mode]
            lcd.grid = getattr(m, 'GRID', Lcd.uniform_grid())
            st = audio.snapshot()
            m.update(st)
            lcd.show(m.render())
            frames += 1
            if time.time() - t_fps > 5:
                print(f"{mode}: {frames / (time.time() - t_fps):.1f} fps, {lcd.sent / (time.time() - t_fps) / 1024:.0f} KB/s")
                frames, lcd.sent, t_fps = 0, 0, time.time()
            time.sleep(max(0, 1 / FPS - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        lcd.close()
        audio.close()


if __name__ == "__main__":
    main()
