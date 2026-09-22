"""An original backing track for the video, synthesised here so it carries no
licence: A minor, 115.2 BPM, so four bars land exactly on the title card.

    python3 music.py     # writes public/music.wav

Sections follow the cut (seconds): 0 to 8.3 intro (pad and arpeggio), the
drop on the title, a groove under the demo, a breakdown from 58.3 (the
agent explainer), back in at 64.6, drums out at the outro, fade to 79.3.
"""

import json
import wave

import numpy as np

# The cut and the music share one grid, in src/timeline.json.
TIMELINE = json.load(open("src/timeline.json"))
SR = 44100
BPM = TIMELINE["bpm"]
BEAT = 60 / BPM
BAR = 4 * BEAT
MARK, _at = {}, 0.0
for _scene in TIMELINE["scenes"]:
    MARK[_scene["name"]] = _at
    _at += _scene["bars"]
TOTAL_BARS = _at
FPS = TIMELINE["fps"]
LENGTH = round(TOTAL_BARS * BAR * FPS) / FPS    # the video, in seconds
N = int(LENGTH * SR) + SR

rng = np.random.default_rng(7)
L = np.zeros(N)
R = np.zeros(N)


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def add(sig, start, pan=0.0, gain=1.0):
    i = int(start * SR)
    if i >= N:
        return
    sig = sig[: N - i]
    L[i:i + len(sig)] += sig * gain * (1 - pan) * 0.5 ** 0.5 * 1.2
    R[i:i + len(sig)] += sig * gain * (1 + pan) * 0.5 ** 0.5 * 1.2


def env(n, a, d, s, r, hold):
    """ADSR over n samples; hold is the sustained part in samples."""
    e = np.zeros(n)
    a, d, r = int(a * SR), int(d * SR), int(r * SR)
    k = 0
    for seg in (np.linspace(0, 1, a, endpoint=False), np.linspace(1, s, d, endpoint=False),
                np.full(max(0, hold - a - d), s), np.linspace(s, 0, r)):
        m = min(len(seg), n - k)
        e[k:k + m] = seg[:m]
        k += m
    return e


def saw_soft(freq, t, harmonics=9, detune=0.0):
    """A warm saw: a few harmonics rolled off, so it needs no filter."""
    out = np.zeros_like(t)
    f = freq * (1 + detune)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * f * h * t) / (h ** 1.35)
    return out


# A minor: Am9, Fmaj7, Cmaj7, G6, one bar each.
CHORDS = [
    (45, [57, 60, 64, 67, 71]),
    (41, [57, 60, 64, 65, 69]),
    (48, [55, 60, 64, 67, 71]),
    (43, [55, 59, 62, 64, 67]),
]


def section(t):
    """0 intro, 1 groove, 2 breakdown, 3 outro."""
    if t < MARK["title"] * BAR:
        return 0
    if MARK["agent"] * BAR <= t < MARK["ask"] * BAR:
        return 2
    if t >= MARK["outro"] * BAR:
        return 3
    return 1


bars = int(LENGTH / BAR) + 1

# Pad: every bar, the chord, slow in and out, a touch of stereo detune.
for b in range(bars):
    start = b * BAR
    root, notes = CHORDS[b % 4]
    n = int(BAR * 1.35 * SR)
    t = np.arange(n) / SR
    e = env(n, 0.35, 0.4, 0.8, 0.9, int(BAR * SR))
    for i, note in enumerate(notes[:4]):
        f = midi(note)
        pan = (-0.5, 0.5, -0.25, 0.25)[i]
        sig = (saw_soft(f, t, 6, -0.0025) + saw_soft(f, t, 6, 0.0025)) * e
        level = 0.055 if section(start) != 2 else 0.07
        add(sig, start, pan, level)

# Bass: the root, on the beat, ducked under the kick.
for b in range(4, bars):
    start = b * BAR
    if section(start) in (2, 3):
        continue
    root = CHORDS[b % 4][0] - 12
    for beat in range(4):
        n = int(BEAT * SR)
        t = np.arange(n) / SR
        e = env(n, 0.01, 0.12, 0.6, 0.12, int(BEAT * 0.8 * SR))
        f = midi(root)
        sig = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t)) * e
        add(sig, start + beat * BEAT, 0, 0.23)

# Arpeggio: eighth notes up and down the chord, plucked, with an echo.
arp_times = []
for b in range(bars):
    start = b * BAR
    root, notes = CHORDS[b % 4]
    order = [0, 2, 4, 3, 1, 3, 4, 2]
    for k in range(8):
        tt = start + k * BEAT / 2
        if tt > LENGTH - 0.5:
            break
        note = notes[order[k]] + 12
        n = int(0.5 * SR)
        t = np.arange(n) / SR
        e = np.exp(-t * 9)
        f = midi(note)
        sig = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t + 0.3)) * e
        sec = section(tt)
        level = {0: 0.10, 1: 0.085, 2: 0.11, 3: 0.09}[sec]
        pan = 0.35 if k % 2 else -0.35
        add(sig, tt, pan, level)
        add(sig, tt + BEAT * 0.75, -pan, level * 0.35)   # dotted-eighth echo
        arp_times.append(tt)

# Drums.
def kick():
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 50 + 110 * np.exp(-t * 35)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(phase) * np.exp(-t * 9)


def hat(decay=60):
    n = int(0.08 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    noise = noise - np.concatenate([[0], noise[:-1]])     # brighter
    return noise * np.exp(-t * decay) * 0.5


def clap():
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    e = np.exp(-t * 25) + 0.6 * np.exp(-np.maximum(0, t - 0.012) * 30) * (t > 0.012)
    return noise * e * 0.35


K, H, CL = kick(), hat(), clap()
duck = np.ones(N)
for b in range(4, bars):
    start = b * BAR
    sec = section(start)
    if sec == 3:
        continue
    for beat in range(4):
        tt = start + beat * BEAT
        if sec == 1 or (sec == 2 and beat == 0 and b % 2 == 0):
            add(K, tt, 0, 0.55)
            i = int(tt * SR)
            m = min(int(0.28 * SR), N - i)
            duck[i:i + m] = np.minimum(duck[i:i + m],
                                       0.55 + 0.45 * np.linspace(0, 1, m) ** 0.6)
        if sec == 1 and beat in (1, 3) and b >= 8:
            add(CL, tt, 0.1, 0.5)
    if sec == 1:
        for k in range(8):
            add(H, start + k * BEAT / 2 + BEAT / 4 * (k % 2 == 1) * 0, 0.4 if k % 2 else -0.2,
                0.18 if k % 2 else 0.1)

# A rising noise swell into the drop on the title card.
drop = MARK["title"] * BAR
n = int(BAR * SR)
t = np.arange(n) / SR
swell = rng.standard_normal(n) * (t / t[-1]) ** 2.5 * 0.12
swell = swell - np.concatenate([[0], swell[:-1]]) * 0.5
add(swell, drop - BAR, 0, 1.0)
# And a soft cymbal-like wash on the drop itself.
n = int(2.5 * SR)
t = np.arange(n) / SR
wash = rng.standard_normal(n)
wash = (wash - np.concatenate([[0], wash[:-1]])) * np.exp(-t * 2.2) * 0.07
add(wash, drop, 0, 1.0)

L *= duck
R *= duck

# A little room: a few quiet, spread reflections.
for delay, g in ((0.031, 0.18), (0.047, 0.15), (0.071, 0.12), (0.113, 0.08)):
    d = int(delay * SR)
    L[d:] += R[:-d] * g
    R[d:] += L[:-d] * g

# Master: fade in a hair, fade out over the last three seconds, soft clip.
end = int(LENGTH * SR)
fade = np.ones(N)
fade[: int(0.3 * SR)] = np.linspace(0, 1, int(0.3 * SR))
fo = int(3.0 * SR)
fade[end - fo:end] = np.linspace(1, 0, fo) ** 1.5
fade[end:] = 0
L, R = np.tanh(L * fade * 1.4), np.tanh(R * fade * 1.4)
peak = max(np.abs(L).max(), np.abs(R).max())
L, R = L / peak * 0.89, R / peak * 0.89

data = (np.stack([L[:end], R[:end]], axis=1) * 32767).astype("<i2")
with wave.open("public/music.wav", "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(data.tobytes())
print(f"wrote public/music.wav, {end / SR:.2f}s")
