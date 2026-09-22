"""An original liquid drum and bass track for the video, synthesised here so it
carries no licence: 172.8 BPM, D minor / F major, two-step break, rolling
sub, lush pads and Rhodes-style stabs in a real reverb.

    python3 music.py     # writes public/music.wav

It follows the cut in src/timeline.json: pads and a filtered break for the
opening, the drop on the title card, the groove under the demo, a breakdown
for the agent scene, back in for "ask", and pads alone to fade on the outro.
"""

import json
import wave

import numpy as np

TIMELINE = json.load(open("src/timeline.json"))
SR = 44100
BPM = TIMELINE["bpm"]
BEAT = 60 / BPM
BAR = 4 * BEAT
STEP = BEAT / 4                     # a sixteenth
MARK, _at = {}, 0.0
for _scene in TIMELINE["scenes"]:
    MARK[_scene["name"]] = _at
    _at += _scene["bars"]
FPS = TIMELINE["fps"]
LENGTH = round(_at * BAR * FPS) / FPS
N = int(LENGTH * SR) + SR
BARS = int(np.ceil(LENGTH / BAR))

# Sections start on whole bars: the cut is on the beat, the music on the bar.
DROP = round(MARK["title"])
BREAK_IN, BREAK_OUT = round(MARK["agent"]), round(MARK["ask"])
OUTRO = round(MARK["outro"])

rng = np.random.default_rng(11)


class Bus:
    def __init__(self):
        self.l = np.zeros(N)
        self.r = np.zeros(N)

    def add(self, sig, start, pan=0.0, gain=1.0):
        i = int(start * SR)
        if i >= N or i < 0:
            return
        sig = sig[: N - i] * gain
        self.l[i:i + len(sig)] += sig * np.sqrt(0.5 * (1 - pan))
        self.r[i:i + len(sig)] += sig * np.sqrt(0.5 * (1 + pan))


drums, sub, keys, pads, fx = Bus(), Bus(), Bus(), Bus(), Bus()
reverb_send = Bus()


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def t_of(seconds):
    return np.arange(int(seconds * SR)) / SR


def section(bar):
    if bar < DROP:
        return "intro"
    if BREAK_IN <= bar < BREAK_OUT:
        return "break"
    if bar >= OUTRO:
        return "outro"
    return "groove"


# Two bars a chord: Bbmaj9, Am9, Gm9, C9sus. Roots, then the voicing.
CHORDS = [
    (34, [58, 62, 65, 69, 72]),
    (33, [57, 60, 64, 67, 71]),
    (31, [58, 62, 65, 67, 69]),
    (36, [58, 62, 64, 67, 70]),
]


def chord_at(bar):
    return CHORDS[(bar // 2) % 4]


# -- pads: slow, wide, soft ------------------------------------------------------

def soft_saw(f, t, harmonics=7, roll=1.6):
    out = np.zeros_like(t)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * f * h * t + h * 0.7) / h ** roll
    return out


for bar in range(0, BARS, 2):
    root, notes = chord_at(bar)
    length = 2 * BAR + 1.2
    t = t_of(length)
    attack, release = 0.6, 1.1
    env = np.minimum(1, t / attack) * np.clip((length - t) / release, 0, 1)
    sec = section(bar)
    level = {"intro": 0.11, "groove": 0.09, "break": 0.13, "outro": 0.12}[sec]
    for i, note in enumerate(notes[:4]):
        f = midi(note)
        wobble = 1 + 0.0025 * np.sin(2 * np.pi * (0.3 + 0.1 * i) * t)
        a = soft_saw(f * wobble * 1.003, t) + soft_saw(f * wobble * 0.997, t)
        pan = [-0.6, 0.6, -0.3, 0.3][i]
        pads.add(a * env, bar * BAR, pan, level)
        reverb_send.add(a * env, bar * BAR, pan, level * 0.9)

# -- keys: a Rhodes-ish electric piano, stabs off the beat -----------------------

def rhodes(f, length=1.4):
    t = t_of(length)
    index = 1.6 * np.exp(-t * 6)
    mod = np.sin(2 * np.pi * f * t)
    tone = np.sin(2 * np.pi * f * t + index * mod)
    tine = 0.25 * np.sin(2 * np.pi * f * 4.01 * t) * np.exp(-t * 18)
    return (tone + tine) * np.exp(-t * 2.4) * np.minimum(1, t / 0.004)


STABS = [0, 6, 10, 14]     # sixteenths in a bar
for bar in range(BARS):
    sec = section(bar)
    root, notes = chord_at(bar)
    for k, step in enumerate(STABS):
        if sec == "intro" and bar < 2 and k:
            continue
        when = bar * BAR + step * STEP
        if when > LENGTH - 0.3:
            break
        vel = [0.9, 0.55, 0.7, 0.5][k] * (1.15 if sec == "break" else 1)
        for i, note in enumerate(notes[1:4]):
            sig = rhodes(midi(note + 12 if k == 3 else note))
            pan = [-0.25, 0.1, 0.3][i]
            keys.add(sig, when + i * 0.006, pan, 0.1 * vel)
            reverb_send.add(sig, when, pan, 0.06 * vel)

# -- sub: a rolling sine that follows the root, sliding into each chord -----------

for bar in range(DROP, BARS):
    sec = section(bar)
    if sec in ("break", "outro"):
        continue
    root, _ = chord_at(bar)
    nxt, _ = chord_at(bar + 1)
    pattern = [(0, 6, root), (6, 4, root), (10, 6, root if bar % 2 == 0 else nxt)]
    for step, steps, note in pattern:
        length = steps * STEP
        t = t_of(length + 0.02)
        f0, f1 = midi(note + 12), midi(note + 12)
        if step == 10 and note != root:
            f0 = midi(root + 12)          # slide up into the next chord
        f = f0 + (f1 - f0) * np.clip(t / (length * 0.6), 0, 1)
        phase = 2 * np.pi * np.cumsum(f) / SR
        env = np.minimum(1, t / 0.01) * np.clip((length - t) / 0.04, 0, 1)
        sig = np.sin(phase) + 0.12 * np.sin(2 * phase)
        sub.add(sig * env, bar * BAR + step * STEP, 0, 0.22)

# -- drums: a two-step break ---------------------------------------------------------

def kick():
    t = t_of(0.3)
    f = 48 + 90 * np.exp(-t * 40)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 11)
    click = rng.standard_normal(len(t)) * np.exp(-t * 400) * 0.25
    return body + click


def snare(bright=1.0):
    t = t_of(0.35)
    tone = np.sin(2 * np.pi * 185 * t) * np.exp(-t * 28) * 0.6
    noise = rng.standard_normal(len(t))
    noise = noise - 0.85 * np.concatenate([[0], noise[:-1]])
    return tone + noise * np.exp(-t * (16 / bright)) * 0.7


def hat(open_=False):
    t = t_of(0.25 if open_ else 0.06)
    noise = rng.standard_normal(len(t))
    for _ in range(2):
        noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.exp(-t * (14 if open_ else 70)) * 0.18


def shaker():
    t = t_of(0.05)
    noise = rng.standard_normal(len(t))
    noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.sin(np.pi * t / t[-1]) * 0.08


K = kick()
duck = np.ones(N)
for bar in range(BARS):
    sec = section(bar)
    start = bar * BAR
    if sec == "outro" and bar > OUTRO:
        continue
    full = sec == "groove"
    intro_hats = sec == "intro" and bar >= 2
    for step in range(16):
        when = start + step * STEP
        if when > LENGTH:
            break
        swing = 0.012 if step % 2 else 0.0
        if full or (sec == "outro" and bar == OUTRO and step == 0):
            if step in (0, 10):
                drums.add(K, when, 0, 0.85)
                i = int(when * SR)
                m = min(int(0.22 * SR), N - i)
                duck[i:i + m] = np.minimum(duck[i:i + m],
                                           0.45 + 0.55 * np.linspace(0, 1, m) ** 0.7)
            if step in (4, 12):
                s = snare()
                drums.add(s, when, 0.05, 0.55)
                reverb_send.add(s, when, 0.05, 0.22)
            if step in (7, 15) and rng.random() < 0.55:
                drums.add(snare(0.6), when + swing, -0.1, 0.12)
        if full or intro_hats or sec == "break":
            if step % 2 == 0:
                drums.add(hat(), when, 0.3, 0.55 if full else 0.35)
            elif full or rng.random() < 0.5:
                drums.add(hat(), when + swing, -0.25, 0.22)
            if full and step == 14 and bar % 4 == 3:
                drums.add(hat(open_=True), when, 0.35, 0.5)
            drums.add(shaker(), when + swing, -0.4 if step % 2 else 0.4,
                      0.6 if full else 0.35)
        if sec == "break" and step in (4, 12):
            rim = snare(0.4)[: int(0.08 * SR)]
            drums.add(rim, when, 0.1, 0.18)
            reverb_send.add(rim, when, 0.1, 0.15)

# -- fx: a riser and a wash into the drop, a wash when the break returns ------------

def riser(seconds):
    t = t_of(seconds)
    noise = rng.standard_normal(len(t))
    noise = noise - 0.6 * np.concatenate([[0], noise[:-1]])
    return noise * (t / t[-1]) ** 3 * 0.25


def wash(seconds=3.0):
    t = t_of(seconds)
    noise = rng.standard_normal(len(t))
    noise = noise - np.concatenate([[0], noise[:-1]])
    return noise * np.exp(-t * 1.6) * 0.12


fx.add(riser(2 * BAR), DROP * BAR - 2 * BAR, 0, 1)
fx.add(wash(), DROP * BAR, 0, 1)
fx.add(riser(BAR), BREAK_OUT * BAR - BAR, 0, 0.8)
fx.add(wash(2.2), BREAK_OUT * BAR, 0, 0.9)

# -- reverb: a long decaying noise impulse, convolved with the send ------------------

def impulse(seconds=2.6, seed=0):
    r = np.random.default_rng(seed)
    t = t_of(seconds)
    noise = r.standard_normal(len(t))
    noise = noise - 0.3 * np.concatenate([[0], noise[:-1]])
    ir = noise * np.exp(-t * 2.3) * np.minimum(1, t / 0.015)
    return ir / np.sqrt((ir ** 2).sum())


def convolve(x, ir):
    size = 1 << int(np.ceil(np.log2(len(x) + len(ir))))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)
    return y[: len(x)]


verb_l = convolve(reverb_send.l, impulse(seed=1))
verb_r = convolve(reverb_send.r, impulse(seed=2))

# -- mix ------------------------------------------------------------------------------

def highpass(x, hz):
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / np.sqrt(1 + (hz / np.maximum(freqs, 1e-3)) ** 4)
    return np.fft.irfft(spec, len(x))


def lowpass(x, hz):
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / np.sqrt(1 + (freqs / hz) ** 4)
    return np.fft.irfft(spec, len(x))


L = drums.l + sub.l + (keys.l + pads.l) * duck + fx.l + verb_l * 0.9 * duck
R = drums.r + sub.r + (keys.r + pads.r) * duck + fx.r + verb_r * 0.9 * duck
L, R = highpass(L, 28), highpass(R, 28)
L, R = lowpass(L, 15000), lowpass(R, 15000)

end = int(LENGTH * SR)
fade = np.ones(N)
fade[: int(0.4 * SR)] = np.linspace(0, 1, int(0.4 * SR))
fo = int(3.0 * SR)
fade[end - fo:end] = np.linspace(1, 0, fo) ** 1.6
fade[end:] = 0
L, R = np.tanh(L * fade * 1.6), np.tanh(R * fade * 1.6)
peak = max(np.abs(L).max(), np.abs(R).max())
L, R = L / peak * 0.89, R / peak * 0.89

data = (np.stack([L[:end], R[:end]], axis=1) * 32767).astype("<i2")
with wave.open("public/music.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(data.tobytes())
print(f"wrote public/music.wav, {end / SR:.2f}s; drop at bar {DROP}, "
      f"break {BREAK_IN}-{BREAK_OUT}, outro {OUTRO}")
