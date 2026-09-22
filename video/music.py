"""An original liquid UK garage track for the video, synthesised here so it
carries no licence and no samples: 132 BPM, swung two-step, soulful ninth
chords on thick detuned pads that pump against the kick, and a warm rolling
sub, in a real reverb.

    python3 music.py     # writes public/music.wav

It follows the cut in src/timeline.json. The bars are laid so the title card
falls on a downbeat (the opening starts with a pickup), then: groove under
the demo, a breakdown for the agent scene, back in for "ask", and pads
alone to fade on the outro.
"""

import json
import math
import wave

import numpy as np

TIMELINE = json.load(open("src/timeline.json"))
SR = 44100
BPM = TIMELINE["bpm"]
BEAT = 60 / BPM
BAR = 4 * BEAT
STEP = BEAT / 4
SWING = 0.58                      # garage shuffle: offbeat sixteenths land late
FPS = TIMELINE["fps"]

_mark, _at = {}, 0.0
for _scene in TIMELINE["scenes"]:
    _mark[_scene["name"]] = _at * BAR           # seconds
    _at += _scene["bars"]
LENGTH = round(_at * BAR * FPS) / FPS
N = int(LENGTH * SR) + SR

# Bar lines are laid so the title starts one.
DROP = math.ceil(_mark["title"] / BAR - 1e-9)
ORIGIN = _mark["title"] - DROP * BAR          # seconds; <= 0, a pickup
BARS = int((LENGTH - ORIGIN) / BAR) + 1


def bar_of(seconds):
    return round((seconds - ORIGIN) / BAR)


BREAK_IN, BREAK_OUT, OUTRO = bar_of(_mark["agent"]), bar_of(_mark["ask"]), bar_of(_mark["outro"])

rng = np.random.default_rng(132)


class Bus:
    def __init__(self):
        self.l = np.zeros(N)
        self.r = np.zeros(N)

    def add(self, sig, start, pan=0.0, gain=1.0):
        i = int(round(start * SR))
        if i < 0:
            sig, i = sig[-i:], 0
        if i >= N or len(sig) == 0:
            return
        sig = sig[: N - i] * gain
        self.l[i:i + len(sig)] += sig * np.sqrt(0.5 * (1 - pan))
        self.r[i:i + len(sig)] += sig * np.sqrt(0.5 * (1 + pan))


drums, bass, keys, pads, fx, send = Bus(), Bus(), Bus(), Bus(), Bus(), Bus()


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def t_of(seconds):
    return np.arange(max(1, int(seconds * SR))) / SR


def at(bar, step=0.0):
    """Time of a sixteenth in a bar, with the shuffle on the offbeats."""
    whole = int(step)
    late = (SWING - 0.5) * 2 * STEP if whole % 2 else 0.0
    return ORIGIN + bar * BAR + step * STEP + late


def section(bar):
    if bar < DROP:
        return "intro"
    if BREAK_IN <= bar < BREAK_OUT:
        return "break"
    if bar >= OUTRO:
        return "outro"
    return "groove"


# ii-V-I-vi in C, soulful: Dm9, G13, Cmaj9, Am9. Bass root, then the voicing.
CHORDS = [
    (38, [53, 57, 60, 64, 65]),
    (43, [53, 57, 59, 64, 65]),
    (36, [52, 55, 59, 62, 64]),
    (33, [55, 60, 64, 67, 71]),
]


def chord(bar):
    return CHORDS[bar % 4]


# -- sounds -------------------------------------------------------------------------

def kick():
    t = t_of(0.35)
    f = 52 + 70 * np.exp(-t * 32)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 8)
    knock = np.sin(2 * np.pi * 180 * t) * np.exp(-t * 60) * 0.3
    return body + knock


def clap():
    t = t_of(0.3)
    noise = rng.standard_normal(len(t))
    noise = noise - 0.9 * np.concatenate([[0], noise[:-1]])
    env = np.zeros_like(t)
    for d in (0.0, 0.009, 0.018):
        env += (t >= d) * np.exp(-np.maximum(0, t - d) * (70 if d < 0.018 else 18))
    snare = np.sin(2 * np.pi * 200 * t) * np.exp(-t * 30) * 0.4
    return noise * env * 0.45 + snare


def rim():
    t = t_of(0.07)
    return (np.sin(2 * np.pi * 820 * t) + 0.6 * np.sin(2 * np.pi * 1600 * t)) * np.exp(-t * 90) * 0.4


def hat(open_=False):
    t = t_of(0.28 if open_ else 0.05)
    noise = rng.standard_normal(len(t))
    for _ in range(2):
        noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.exp(-t * (12 if open_ else 85)) * 0.1


def shaker():
    t = t_of(0.06)
    noise = rng.standard_normal(len(t))
    noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.sin(np.pi * t / t[-1]) ** 2 * 0.05


# -- arrangement ----------------------------------------------------------------------

# Pads: the whole harmony. Seven detuned saws a note, the full voicing plus
# the root an octave down, wide, into the reverb, pumping against the kick.
_voices = {}


def thick(n, length):
    """One note of the pad, cached by pitch: every bar reuses it."""
    key = (n, round(length, 3))
    if key not in _voices:
        t = t_of(length)
        f = midi(n)
        left, right = np.zeros_like(t), np.zeros_like(t)
        for v, detune in enumerate(np.linspace(-0.011, 0.011, 7)):
            ff = f * (1 + detune)
            phase = rng.random() * 6.28
            wave_ = np.zeros_like(t)
            for h in range(1, 9):
                wave_ += np.sin(2 * np.pi * ff * h * t + phase * h) / h ** 1.25
            pan = (v - 3) / 3
            left += wave_ * np.sqrt(0.5 * (1 - pan))
            right += wave_ * np.sqrt(0.5 * (1 + pan))
        _voices[key] = (left / 7, right / 7)
    return _voices[key]


for bar in range(BARS):
    root, notes = chord(bar)
    sec = section(bar)
    level = {"intro": 0.24, "groove": 0.22, "break": 0.26, "outro": 0.25}[sec]
    length = BAR + 0.9
    t = t_of(length)
    swell = np.minimum(1, t / (0.25 if sec == "groove" else 0.6))
    env = swell * np.clip((length - t) / 0.9, 0, 1)
    for n in notes + [root + 12]:
        left, right = thick(n, length)
        i = int(round(at(bar) * SR))
        a, b = (left * env, right * env)
        if i < 0:
            a, b, i = a[-i:], b[-i:], 0
        m = min(len(a), N - i)
        if m <= 0:
            continue
        gain = level * (0.7 if n == root + 12 else 1.0)
        pads.l[i:i + m] += a[:m] * gain
        pads.r[i:i + m] += b[:m] * gain
        send.l[i:i + m] += a[:m] * gain * 0.7
        send.r[i:i + m] += b[:m] * gain * 0.7

# Sub: warm and rolling, syncopated, with a slide into each new root.
for bar in range(DROP, BARS):
    if section(bar) in ("break", "outro"):
        continue
    root, _ = chord(bar)
    nxt, _ = chord(bar + 1)
    pattern = [(0, 3, root), (3, 3, root + 12), (6, 4, root), (10, 3, root), (13, 3, nxt)]
    for step, steps, note in pattern:
        length = steps * STEP
        t = t_of(length + 0.02)
        f1 = midi(note + 12)
        f0 = midi(root + 12) if note == nxt and nxt != root else f1
        f = f0 + (f1 - f0) * np.clip(t / (length * 0.5), 0, 1)
        phase = 2 * np.pi * np.cumsum(f) / SR
        env = np.minimum(1, t / 0.008) * np.clip((length - t) / 0.03, 0, 1)
        sig = np.tanh(1.4 * (np.sin(phase) + 0.2 * np.sin(2 * phase)))
        bass.add(sig * env, at(bar, step), 0, 0.12)

# Drums: two-step. Kick on the one and the shuffled "a" of three; clap on
# two and four; rimshots and shuffled hats between.
K, duck = kick(), np.ones(N)
for bar in range(BARS):
    sec = section(bar)
    groove = sec == "groove"
    if sec == "outro" and bar > OUTRO:
        continue
    for step in range(16):
        when = at(bar, step)
        if when > LENGTH:
            break
        if groove or (sec == "outro" and bar == OUTRO and step == 0):
            if step in (0, 10) or (step == 7 and bar % 4 == 3):
                drums.add(K, when, 0, 0.6)
                i = int(max(0, when) * SR)
                m = min(int(0.2 * SR), N - i)
                duck[i:i + m] = np.minimum(duck[i:i + m], 0.35 + 0.65 * np.linspace(0, 1, m) ** 0.8)
        if groove and step in (4, 12):
            c = clap()
            drums.add(c, when, 0.05, 0.6)
            send.add(c, when, 0.05, 0.25)
        if groove and step in (3, 13) and rng.random() < 0.7:
            drums.add(rim(), when, -0.2, 0.35)
        if groove or sec == "break" or (sec == "intro" and bar >= DROP - 2):
            if step % 4 == 2:
                drums.add(hat(open_=groove and bar % 2 == 1 and step == 14), when, 0.3,
                          0.9 if groove else 0.6)
            elif step % 2 == 1:
                drums.add(hat(), when, -0.25, 0.45)
            drums.add(shaker(), when, 0.4 if step % 2 else -0.4, 0.9 if groove else 0.5)
        if sec == "break" and step in (4, 12):
            drums.add(rim(), when, 0.1, 0.3)
            send.add(rim(), when, 0.1, 0.3)

# A soft riser into the drop and into the return, a wash on each.
def riser(seconds):
    t = t_of(seconds)
    noise = rng.standard_normal(len(t))
    noise = noise - 0.5 * np.concatenate([[0], noise[:-1]])
    return noise * (t / t[-1]) ** 3 * 0.12


def wash(seconds=2.5):
    t = t_of(seconds)
    noise = rng.standard_normal(len(t))
    noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.exp(-t * 1.8) * 0.06


fx.add(riser(BAR), at(DROP) - BAR, 0, 1)
fx.add(wash(), at(DROP), 0, 1)
fx.add(riser(BAR), at(BREAK_OUT) - BAR, 0, 0.8)
fx.add(wash(2.0), at(BREAK_OUT), 0, 0.8)

# -- reverb and mix ---------------------------------------------------------------------

def impulse(seconds=2.2, seed=0):
    r = np.random.default_rng(seed)
    t = t_of(seconds)
    noise = r.standard_normal(len(t))
    ir = noise * np.exp(-t * 2.6) * np.minimum(1, t / 0.02)
    return ir / np.sqrt((ir ** 2).sum())


def convolve(x, ir):
    size = 1 << int(np.ceil(np.log2(len(x) + len(ir))))
    return np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]


def shelf(x, low_hz=None, high_hz=None):
    spec = np.fft.rfft(x)
    f = np.maximum(np.fft.rfftfreq(len(x), 1 / SR), 1e-3)
    if low_hz:
        spec *= 1 / np.sqrt(1 + (low_hz / f) ** 4)
    if high_hz:
        spec *= 1 / np.sqrt(1 + (f / high_hz) ** 4)
    return np.fft.irfft(spec, len(x))


verb_l = shelf(convolve(send.l, impulse(seed=3)), 180, 7000)
verb_r = shelf(convolve(send.r, impulse(seed=4)), 180, 7000)
hats_l, hats_r = shelf(drums.l, None, 11000), shelf(drums.r, None, 11000)   # no fizz

_g = slice(int(12 * SR), int(20 * SR))
for _name, _bus in (("drums", drums), ("bass", bass), ("pads", pads)):
    print(f"  {_name:6} {20 * np.log10(np.sqrt((_bus.l[_g] ** 2).mean()) + 1e-9):6.1f} dB")
L = hats_l + bass.l + (keys.l + pads.l + verb_l * 0.8) * duck + fx.l
R = hats_r + bass.r + (keys.r + pads.r + verb_r * 0.8) * duck + fx.r
L, R = shelf(L, 30, 16000), shelf(R, 30, 16000)

end = int(LENGTH * SR)
fade = np.ones(N)
fade[: int(0.3 * SR)] = np.linspace(0, 1, int(0.3 * SR))
fo = int(3.0 * SR)
fade[end - fo:end] = np.linspace(1, 0, fo) ** 1.5
fade[end:] = 0
L, R = L * fade, R * fade
peak = max(np.abs(L).max(), np.abs(R).max())
L, R = np.tanh(L / peak * 1.1) / np.tanh(1.1), np.tanh(R / peak * 1.1) / np.tanh(1.1)
L, R = L * 0.89, R * 0.89

data = (np.stack([L[:end], R[:end]], axis=1) * 32767).astype("<i2")
with wave.open("public/music.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(data.tobytes())
print(f"wrote public/music.wav, {end / SR:.2f}s at {BPM} BPM; drop bar {DROP} "
      f"({ORIGIN:+.2f}s origin), break {BREAK_IN}-{BREAK_OUT}, outro {OUTRO}")
