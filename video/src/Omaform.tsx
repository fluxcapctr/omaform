import React from 'react';
import {
  AbsoluteFill, Audio, Easing, Img, Sequence, interpolate, random, spring, staticFile,
  useCurrentFrame, useVideoConfig,
} from 'remotion';
import {C, FONT} from './theme';
import targets from '../public/shots/targets.json';
import icon from '../public/icon-cells.json';
import timeline from './timeline.json';

type Box = [number, number, number, number];
type Shot = keyof typeof targets;

const fontFace = `
@font-face { font-family: '${FONT}'; src: url('${staticFile('fonts/JetBrainsMono-Regular.ttf')}'); font-weight: 400; }
@font-face { font-family: '${FONT}'; src: url('${staticFile('fonts/JetBrainsMono-Bold.ttf')}'); font-weight: 700; }
`;

const W = 1920;
const H = 1080;
// The music's grid: 172.8 BPM is 41.67 frames a bar, 10.42 a beat.
const BAR = (timeline.fps * 60 * 4) / timeline.bpm;
const MUSIC_BEAT = BAR / 4;
// Choreography moves in steps of one and a half beats (15.625 frames), which
// lands every other step on a beat without being frantic at drum and bass tempo.
const BEAT = MUSIC_BEAT * 1.5;
const ease = Easing.bezier(0.45, 0, 0.2, 1);
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

/** 1 on each snare (beats two and four), falling away before the next. */
const SNARE = MUSIC_BEAT * 2;
const pulse = (frame: number) =>
  Math.exp(-(((frame - MUSIC_BEAT + SNARE) % SNARE) / SNARE) * 5);

// -- helpers -----------------------------------------------------------------

function box(shot: Shot, key: string): Box | null {
  const t = targets[shot] as Record<string, unknown>;
  const v = t[key];
  if (!Array.isArray(v) || v.length === 0) return null;
  if (Array.isArray(v[0])) {
    const bs = (v as Box[]).filter(Boolean);
    const x0 = Math.min(...bs.map((b) => b[0]));
    const y0 = Math.min(...bs.map((b) => b[1]));
    const x1 = Math.max(...bs.map((b) => b[0] + b[2]));
    const y1 = Math.max(...bs.map((b) => b[1] + b[3]));
    return [x0, y0, x1 - x0, y1 - y0];
  }
  return v as Box;
}

const pad = (b: Box, px: number, py = px): Box =>
  [b[0] - px, b[1] - py, b[2] + 2 * px, b[3] + 2 * py];

function camera(shot: Shot, b: Box | null) {
  const {width, height} = targets[shot];
  const view: Box = b ?? [0, 0, width, height];
  const scale = Math.min(W / view[2], H / view[3]);
  const cx = view[0] + view[2] / 2;
  const cy = view[1] + view[3] / 2;
  let x = W / 2 - cx * scale;
  let y = H / 2 - cy * scale;
  x = Math.min(0, Math.max(W - width * scale, x));
  y = Math.min(0, Math.max(H - height * scale, y));
  if (width * scale < W) x = (W - width * scale) / 2;
  if (height * scale < H) y = (H - height * scale) / 2;
  return {scale, x, y};
}

// -- the pixel language ----------------------------------------------------------

/** Squares drifting up the frame, lighting on the beat. Omarchy's grid, moving. */
const Pixels: React.FC<{count?: number; seed?: string}> = ({count = 26, seed = 'px'}) => {
  const frame = useCurrentFrame();
  const p = pulse(frame);
  return (
    <AbsoluteFill style={{overflow: 'hidden'}}>
      {Array.from({length: count}).map((_, i) => {
        const size = 10 + Math.floor(random(`${seed}s${i}`) * 4) * 10;
        const x = random(`${seed}x${i}`) * W;
        const speed = 0.4 + random(`${seed}v${i}`) * 1.2;
        const y = ((random(`${seed}y${i}`) * (H + 200) - frame * speed) % (H + 200) + H + 200)
          % (H + 200) - 100;
        const lit = random(`${seed}l${i}`) < 0.25;
        return (
          <div key={i} style={{
            position: 'absolute', left: x, top: y, width: size, height: size,
            background: lit ? C.accent : C.muted,
            opacity: lit ? 0.18 + 0.5 * p : 0.12 + 0.06 * p,
          }} />
        );
      })}
    </AbsoluteFill>
  );
};

/** The icon, assembled cell by cell from scattered squares. */
const PixelLogo: React.FC<{size: number; delay?: number}> = ({size, delay = 0}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const u = size / 16;
  const cells: {x: number; y: number; w: number; h: number; c: string; i: number}[] = [];
  let i = 0;
  for (const g of icon.groups) {
    for (const [x, y, w, h] of g.cells as number[][]) {
      for (let dx = 0; dx < w; dx++) {
        for (let dy = 0; dy < h; dy++) {
          cells.push({x: x + dx, y: y + dy, w: 1, h: 1, c: g.colour, i: i++});
        }
      }
    }
  }
  const tile = spring({frame: frame - delay, fps, config: {damping: 14}});
  return (
    <div style={{position: 'relative', width: size, height: size,
      transform: `scale(${0.6 + 0.4 * tile})`, opacity: tile}}>
      <div style={{position: 'absolute', inset: 0, background: icon.tile}} />
      {cells.map((c) => {
        const s = spring({frame: frame - delay - 4 - (c.i % 24) * 0.6, fps,
          config: {damping: 13, stiffness: 140}});
        const fx = (random(`lx${c.i}`) - 0.5) * size * 2.4;
        const fy = (random(`ly${c.i}`) - 0.5) * size * 2.4;
        return (
          <div key={c.i} style={{
            position: 'absolute', left: c.x * u, top: c.y * u, width: u + 0.5, height: u + 0.5,
            background: c.c, opacity: s,
            transform: `translate(${(1 - s) * fx}px, ${(1 - s) * fy}px)`,
          }} />
        );
      })}
    </div>
  );
};

/** Text that types itself, with Omarchy's block cursor. */
const Typed: React.FC<{text: string; start?: number; cps?: number; style?: React.CSSProperties}> = (
  {text, start = 0, cps = 1.4, style}) => {
  const frame = useCurrentFrame();
  const n = Math.max(0, Math.floor((frame - start) * cps));
  const done = n >= text.length;
  const blink = done ? (Math.floor(frame / BEAT) % 2 === 0 ? 1 : 0) : 1;
  return (
    <span style={style}>
      {text.slice(0, n)}
      <span style={{display: 'inline-block', width: '0.55em', height: '1em',
        background: C.accent, marginLeft: 4, verticalAlign: '-0.12em', opacity: blink}} />
    </span>
  );
};

// -- screens ---------------------------------------------------------------------

type Move = {at: number; to: Box | null};

const ShotView: React.FC<{
  shot: Shot; moves: Move[]; rings?: {from: number; box: Box}[]; dur?: number;
}> = ({shot, moves, rings = [], dur = 120}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const {width, height} = targets[shot];
  const cams = moves.map((m) => camera(shot, m.to));
  let cam = cams[0];
  for (let i = 1; i < moves.length; i++) {
    const p = spring({frame: frame - moves[i].at, fps,
      config: {damping: 19, stiffness: 110, mass: 0.9}});
    if (p <= 0.0001) break;
    const a = cam;
    const b = cams[i];
    cam = {scale: a.scale + (b.scale - a.scale) * p, x: a.x + (b.x - a.x) * p,
      y: a.y + (b.y - a.y) * p};
  }
  // Never still: a slow push in, a small sway in depth.
  const push = 1 + 0.045 * interpolate(frame, [0, dur], [0, 1], clamp);
  const enter = spring({frame, fps, config: {damping: 18}});
  const rotY = (1 - enter) * -14 + Math.sin(frame / 38) * 1.4;
  const rotX = (1 - enter) * 6 + Math.cos(frame / 47) * 0.9;
  return (
    <AbsoluteFill style={{background: C.bgDark, overflow: 'hidden', perspective: 1800}}>
      <Pixels seed={shot} count={18} />
      <AbsoluteFill style={{transform: `scale(${push}) rotateY(${rotY}deg) rotateX(${rotX}deg)`,
        transformOrigin: '50% 50%'}}>
        <div style={{position: 'absolute', left: cam.x, top: cam.y,
          width: width * cam.scale, height: height * cam.scale,
          boxShadow: '0 40px 120px rgba(0,0,0,0.6)'}}>
          <Img src={staticFile(`shots/${shot}.png`)}
            style={{width: '100%', height: '100%', display: 'block'}} />
          {rings.map((r, i) => {
            const s = spring({frame: frame - r.from, fps, config: {damping: 11, stiffness: 160}});
            const b = r.box;
            const beat = pulse(frame);
            return (
              <div key={i} style={{
                position: 'absolute', left: b[0] * cam.scale, top: b[1] * cam.scale,
                width: b[2] * cam.scale, height: b[3] * cam.scale,
                border: `4px solid ${C.accent}`, opacity: s,
                transform: `scale(${1.35 - 0.35 * s})`,
                boxShadow: `0 0 0 ${6 + 10 * beat}px rgba(250,169,104,${0.12 + 0.2 * beat})`,
              }} />
            );
          })}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Caption: React.FC<{title: string; body?: string; delay?: number}> = (
  {title, body, delay = 6}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame: frame - delay, fps, config: {damping: 16, stiffness: 130}});
  const words = title.split(' ');
  return (
    <div style={{position: 'absolute', left: 80, bottom: 70, maxWidth: 1300,
      transform: `translateX(${(1 - s) * -160}px)`, opacity: Math.min(1, s * 1.4),
      background: 'rgba(2,12,23,0.9)', padding: '24px 34px 26px 44px', fontFamily: FONT}}>
      <div style={{position: 'absolute', left: 0, top: 0, bottom: 0, width: 10,
        background: C.accent, transform: `scaleY(${s})`, transformOrigin: 'top'}} />
      <div style={{color: C.accent, fontSize: 50, fontWeight: 700, lineHeight: 1.15}}>
        {words.map((w, i) => {
          const ws = spring({frame: frame - delay - 3 - i * 2.2, fps,
            config: {damping: 12, stiffness: 180}});
          return (
            <span key={i} style={{display: 'inline-block', marginRight: '0.45em',
              transform: `translateY(${(1 - ws) * 26}px) scale(${0.85 + 0.15 * ws})`,
              opacity: ws}}>{w}</span>
          );
        })}
      </div>
      {body && <div style={{color: C.dim, fontSize: 28, marginTop: 10, lineHeight: 1.4,
        opacity: interpolate(frame, [delay + 12, delay + 22], [0, 1], clamp)}}>{body}</div>}
    </div>
  );
};

/** In fast, out fast: every scene arrives with a push and leaves with a pull. */
const Scene: React.FC<{dur: number; children: React.ReactNode}> = ({dur, children}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const s = spring({frame, fps, config: {damping: 20, stiffness: 170}});
  const out = interpolate(frame, [dur - 7, dur], [0, 1], clamp);
  return (
    <AbsoluteFill style={{
      opacity: Math.min(1, s * 1.5) * (1 - out),
      transform: `translateX(${(1 - s) * 140 - out * 90}px) scale(${1.06 - 0.06 * s + out * 0.05})`,
      filter: out > 0 ? `blur(${out * 6}px)` : undefined,
    }}>{children}</AbsoluteFill>
  );
};

/** A column of orange squares that sweeps across at each cut. */
const Wipe: React.FC = () => {
  const frame = useCurrentFrame();
  const x = interpolate(frame, [0, 12], [-200, W + 200], {...clamp, easing: ease});
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      {Array.from({length: 12}).map((_, i) => (
        <div key={i} style={{position: 'absolute', top: i * 90, width: 90, height: 90,
          left: x - (i % 3) * 60 - random(`w${i}`) * 80, background: C.accent,
          opacity: 0.9 - (i % 3) * 0.25}} />
      ))}
    </AbsoluteFill>
  );
};

// -- scenes ----------------------------------------------------------------------

const FORMS = ['fw9.pdf', 'fw4.pdf', 'i-9.pdf', 'rental-application.pdf',
  'permission-slip.pdf', 'volunteer-form.docx', 'patient-intake.pdf', 'vendor-setup.pdf',
  'lease-renewal.pdf'];

const LINES = ['Name.', 'Address.', 'Social Security number.', 'Date. Signature.',
  'Print. Sign. Scan.'];

const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const head = spring({frame, fps, config: {damping: 14}});
  // Bars one and two: the forms pile in. Bar three: they fall away and the
  // same questions come in. Bar four: struck out, on the beat.
  const fall = interpolate(frame, [BEAT * 7, BEAT * 8.5], [0, 1], {...clamp, easing: ease});
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT}}>
      <Pixels seed="problem" />
      <AbsoluteFill style={{padding: '110px 120px'}}>
        <div style={{color: C.accent, fontSize: 76, fontWeight: 700, lineHeight: 1.15,
          transform: `translateY(${(1 - head) * 40}px) scale(${0.92 + 0.08 * head})`,
          transformOrigin: 'left top', opacity: head}}>
          Stop filling out the same forms<br />over and over again.
        </div>
        <div style={{position: 'relative', marginTop: 50, flex: 1}}>
          <div style={{display: 'flex', flexWrap: 'wrap', gap: 18, maxWidth: 1600,
            position: 'absolute', inset: 0}}>
            {FORMS.map((f, i) => {
              const s = spring({frame: frame - BEAT * 1.2 - i * (BEAT / 2), fps,
                config: {damping: 11, stiffness: 150}});
              const fx = (random(`fx${i}`) - 0.5) * 1400;
              const fy = (random(`fy${i}`) - 0.2) * 900;
              const rot = (random(`fr${i}`) - 0.5) * 50;
              const drop = fall * (700 + random(`fd${i}`) * 400);
              const spin = fall * (random(`fs${i}`) - 0.5) * 70;
              return (
                <div key={f} style={{color: C.dim, fontSize: 36, padding: '16px 24px',
                  height: 'fit-content', border: `2px solid ${C.muted}`, background: C.panel,
                  opacity: Math.min(1, s * 2) * (1 - fall * 0.6),
                  transform: `translate(${(1 - s) * fx}px, ${(1 - s) * fy + drop}px) ` +
                    `rotate(${(1 - s) * rot + spin}deg)`}}>
                  {f}
                </div>
              );
            })}
          </div>
          <div style={{position: 'absolute', inset: 0}}>
            {LINES.map((l, i) => {
              const at = BEAT * (8.5 + i * 0.6);
              const on = spring({frame: frame - at, fps, config: {damping: 12, stiffness: 200}});
              const hit = BEAT * (12 + i * 0.5);
              const strike = interpolate(frame, [hit, hit + 5], [0, 100], clamp);
              const shake = frame >= hit && frame < hit + 6 ? Math.sin(frame * 3) * 6 : 0;
              return (
                <div key={l} style={{position: 'relative', color: C.fg, fontSize: 54,
                  fontWeight: 700, marginBottom: 8, width: 'fit-content', opacity: on,
                  transform: `translateX(${(1 - on) * -80 + shake}px)`}}>
                  {l}
                  {strike > 0 && <div style={{position: 'absolute', left: -6, top: '50%',
                    height: 8, width: `calc(${strike}% + ${(12 * strike) / 100}px)`,
                    background: C.red}} />}
                </div>
              );
            })}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Title: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT}}>
      <Pixels seed="title" count={40} />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 44,
          transform: `scale(${1 + 0.02 * pulse(frame)})`}}>
          <PixelLogo size={190} />
          <Typed text="Omaform" start={10} cps={0.7}
            style={{color: C.fg, fontSize: 150, fontWeight: 700, letterSpacing: -4}} />
        </div>
        <div style={{color: C.accent, fontSize: 50, marginTop: 44,
          opacity: interpolate(frame, [22, 30], [0, 1], clamp),
          transform: `translateY(${interpolate(frame, [22, 32], [20, 0], clamp)}px)`}}>
          Open a form. It's already filled.
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const AGENTS = ['claude', 'codex', 'opencode', 'a local model'];

const Agent: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const a = spring({frame, fps, config: {damping: 13}});
  const b = spring({frame: frame - BEAT * 3, fps, config: {damping: 13}});
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT}}>
      <Pixels seed="agent" count={30} />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
        <div style={{color: C.fg, fontSize: 92, fontWeight: 700,
          transform: `scale(${0.8 + 0.2 * a})`, opacity: a}}>
          Confusing document?
        </div>
        <div style={{color: C.accent, fontSize: 92, fontWeight: 700, marginTop: 10,
          transform: `translateY(${(1 - b) * 50}px)`, opacity: b}}>
          Ask your Omarchy agent.
        </div>
        <div style={{display: 'flex', gap: 18, marginTop: 60}}>
          {AGENTS.map((name, i) => {
            const s = spring({frame: frame - BEAT * (4.5 + i * 0.5), fps,
              config: {damping: 10, stiffness: 180}});
            return (
              <div key={name} style={{color: C.dim, fontSize: 34, padding: '14px 24px',
                border: `2px solid ${C.muted}`, background: C.panel, opacity: s,
                transform: `translateY(${(1 - s) * 60}px) scale(${0.7 + 0.3 * s})`}}>
                {name}
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const items = ['PDF forms', 'flat PDFs', 'scans', 'Word documents', 'signatures',
    'encrypted vault', 'black-outs', 'page tools'];
  return (
    <AbsoluteFill style={{background: C.bg, fontFamily: FONT}}>
      <Pixels seed="outro" count={40} />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 32}}>
          <PixelLogo size={130} />
          <div style={{color: C.fg, fontSize: 110, fontWeight: 700}}>Omaform</div>
        </div>
        <div style={{display: 'flex', flexWrap: 'wrap', justifyContent: 'center', gap: 14,
          maxWidth: 1400, marginTop: 44}}>
          {items.map((it, i) => {
            const s = spring({frame: frame - 8 - i * (BEAT / 2), fps,
              config: {damping: 10, stiffness: 190}});
            return (
              <div key={it} style={{color: C.dim, fontSize: 30, padding: '10px 18px',
                border: `2px solid ${C.muted}`, opacity: s,
                transform: `scale(${0.6 + 0.4 * s})`}}>{it}</div>
            );
          })}
        </div>
        <div style={{color: C.accent, fontSize: 44, marginTop: 54,
          opacity: interpolate(frame, [BEAT * 5, BEAT * 5 + 8], [0, 1], clamp)}}>
          Fill it out once and for all.
        </div>
        <div style={{color: C.dim, fontSize: 30, marginTop: 20,
          opacity: interpolate(frame, [BEAT * 6, BEAT * 6 + 8], [0, 1], clamp)}}>
          github.com/fluxcapctr/omaform
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// -- the cut -------------------------------------------------------------------------

const b = box;
const S = (name: string) => name as Shot;
const w9 = S('w9_fill');

const SCENES: Record<string, (dur: number) => React.ReactNode> = {
  problem: () => <Problem />,
  title: () => <Title />,
  w9: (dur) => <>
    <ShotView dur={dur} shot={w9} moves={[
      {at: 0, to: null},
      {at: 22, to: pad(b(w9, 'preview')!, 0, -20)},
      {at: 62, to: pad([100, 300, 1000, 420], 20)},
    ]} rings={[
      {from: 76, box: pad(b(w9, 'name')!, 4)},
      {from: 76 + BEAT, box: pad(b(w9, 'tick')!, 5)},
      {from: 76 + 2 * BEAT, box: pad(b(w9, 'address')!, 4)},
    ]} />
    <Caption title="Open the W-9. It's filled." delay={10}
      body="Name, address, tax classification: read off the page like a person would." />
  </>,
  list: (dur) => <>
    <ShotView dur={dur} shot={w9} moves={[{at: 0, to: b(w9, 'list')}]} />
    <Sequence from={Math.round(BEAT * 4)}>
      <ShotView dur={dur} shot={S('w9_sign')} moves={[
        {at: 0, to: pad(b(S('w9_sign'), 'ssn')!, 520, 260)},
        {at: 8, to: pad(b(S('w9_sign'), 'ssn')!, 360, 170)}]}
        rings={[{from: 14, box: pad(b(S('w9_sign'), 'ssn')!, 5)}]} />
    </Sequence>
    <Caption title="Omaform fills in only what it's sure of."
      body="The rest stays empty on purpose." />
  </>,
  vault: (dur) => <>
    <ShotView dur={dur} shot={S('vault')} moves={[
      {at: 0, to: null},
      {at: 10, to: pad([577, 297, 370, 246], 150, 90)},
      {at: Math.round(BEAT * 4.5), to: [540, 280, 984, 553]},
    ]} rings={[
      {from: 20, box: [596, 423, 332, 36]},
      {from: Math.round(BEAT * 5), box: [1165, 540, 36, 250]},
    ]} />
    <Caption title="Your private data, locked in an encrypted vault."
      body="Protected by a passphrase only you know. Opened only when a form needs it." />
  </>,
  hover: (dur) => <>
    <ShotView dur={dur} shot={S('w9_hover')} moves={[{at: 0, to: null},
      {at: 8, to: pad([0, 280, 1143, 300], 10)}]} />
    <Caption title="Hover a row, see the box." body="Click to change. Tab to type." />
  </>,
  sigdialog: (dur) => <>
    <ShotView dur={dur} shot={S('signature_dialog')} moves={[{at: 0, to: null},
      {at: 10, to: [300, 150, 950, 600]}]} />
    <Caption title="Draw your signature once." />
  </>,
  sign: (dur) => <>
    <ShotView dur={dur} shot={S('w9_sign')} moves={[
      {at: 0, to: b(S('w9_sign'), 'preview')},
      {at: 12, to: pad(b(S('w9_sign'), 'signature')!, 260, 120)},
    ]} rings={[
      {from: 24, box: pad(b(S('w9_sign'), 'signature')!, 4)},
      {from: 24 + BEAT, box: pad(b(S('w9_sign'), 'date')!, 4)},
    ]} />
    <Caption title="Signed. Dated. Drag it anywhere." />
  </>,
  identities: (dur) => <>
    <ShotView dur={dur} shot={S('identities')} moves={[{at: 0, to: null},
      {at: 10, to: [280, 140, 1000, 640]}]} />
    <Caption title="Store multiple identities." body="You. Your business. Your family." />
  </>,
  flat: (dur) => <>
    <ShotView dur={dur} shot={S('flat')} moves={[{at: 0, to: null},
      {at: 12, to: pad(b(S('flat'), 'preview')!, 0, -40)}]} />
    <Sequence from={Math.round(BEAT * 5)}>
      <ShotView dur={dur} shot={S('flat_sign')} moves={[
        {at: 0, to: pad(b(S('flat_sign'), 'signature')!, 600, 300)},
        {at: 6, to: pad(b(S('flat_sign'), 'signature')!, 380, 180)}]} />
    </Sequence>
    <Caption title="No form fields? Still filled."
      body="Lines on a page, scans, anything." />
  </>,
  i9: (dur) => <>
    <ShotView dur={dur} shot={S('i9_fill')} moves={[
      {at: 0, to: null},
      {at: 12, to: pad(b(S('i9_fill'), 'preview')!, 0, -20)},
    ]} rings={[{from: 30, box: pad(b(S('i9_fill'), 'tick')!, 5)}]} />
    <Caption title="Knows whose section is whose."
      body="Your part of the I-9. Not your employer's." />
  </>,
  blackout: (dur) => <>
    <ShotView dur={dur} shot={S('blackout')} moves={[
      {at: 0, to: pad(b(S('blackout'), 'box')!, 700, 380)},
      {at: 8, to: pad(b(S('blackout'), 'box')!, 420, 220)}]}
      rings={[{from: 16, box: pad(b(S('blackout'), 'box')!, 6)}]} />
    <Caption title="Black it out. For real." body="Removed, not covered." />
  </>,
  pages: (dur) => <>
    <ShotView dur={dur} shot={S('pages')} moves={[{at: 0, to: null},
      {at: 10, to: [0, 50, 1000, 560]}]} />
    <Caption title="Merge. Reorder. Rotate your documents." />
  </>,
  agent: () => <Agent />,
  ask: (dur) => <>
    <ShotView dur={dur} shot={S('ask_model')} moves={[{at: 0, to: null},
      {at: 8, to: [300, 150, 950, 600]}]} />
    <Sequence from={Math.round(BEAT * 4)}>
      <ShotView dur={dur} shot={S('model_busy')} moves={[{at: 0, to: [900, 57, 624, 351]}]}
        rings={[{from: 8, box: [1144, 100, 380, 150]}]} />
    </Sequence>
    <Caption title="Only when you ask." body="It sees the questions. Never your answers." />
  </>,
  docx: (dur) => <>
    <ShotView dur={dur} shot={S('docx')} moves={[{at: 0, to: null},
      {at: 10, to: b(S('docx'), 'list')}]} />
    <Caption title="Word documents too." />
  </>,
  outro: () => <Outro />,
};

// Scene starts on the music's grid, in frames.
const starts: number[] = [];
{
  let bars = 0;
  for (const s of timeline.scenes) {
    starts.push(Math.round(bars * BAR));
    bars += s.bars;
  }
  starts.push(Math.round(bars * BAR));
}

export const TOTAL = starts[starts.length - 1];

export const Omaform: React.FC = () => (
  <AbsoluteFill style={{background: C.bgDark}}>
    <style>{fontFace}</style>
    {/* An original track from music.py, on the same grid as the cuts. */}
    <Audio src={staticFile('music.wav')} volume={0.85} />
    {timeline.scenes.map((s, i) => {
      const dur = starts[i + 1] - starts[i];
      return (
        <Sequence key={s.name} from={starts[i]} durationInFrames={dur}>
          <Scene dur={dur}>{SCENES[s.name](dur)}</Scene>
        </Sequence>
      );
    })}
    {starts.slice(1, -1).map((at, i) => (
      <Sequence key={`w${i}`} from={at - 6} durationInFrames={14}>
        <Wipe />
      </Sequence>
    ))}
  </AbsoluteFill>
);
