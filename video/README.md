# The Omaform video

A 69-second walkthrough made with [Remotion](https://www.remotion.dev), from
real screenshots of the app filled with made-up details (Alex Rivera, Rivera
Design Co, SSN 123-45-6789).

```console
../.venv/bin/python capture.py   # from the repo root: ./.venv/bin/python video/capture.py
python3 music.py                 # the soundtrack, public/music.wav
npm install
npm run studio                   # preview and tweak
npm run render                   # writes out/omaform.mp4
```

`capture.py` runs the real window against a throwaway data folder, drives it
through each feature, and has GTK render the window to PNG at twice its size.
It also writes `public/shots/targets.json`, the window positions of the
things each scene zooms to, so a recapture keeps the zooms on target.
Rendering uses the system Chromium (`remotion.config.ts`).

The music is original, synthesised by `music.py` with numpy (A minor, 115.2
BPM, so four bars land on the title card), so it carries no licence. It
follows the cut: intro, a drop on the title, a groove under the demo, a
breakdown for the agent scene, and a fade on the outro. Scene lengths live in
`src/timeline.json`, in bars of the music, and both the cut and `music.py`
read them, so changing a scene's length keeps the music on the cuts.
