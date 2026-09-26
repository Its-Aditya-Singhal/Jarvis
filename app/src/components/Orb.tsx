import { useEffect, useRef } from "react";

export type OrbMode = "idle" | "scanning" | "approved" | "denied" | "locked" | "offline";

const PALETTE: Record<OrbMode, [number, number, number]> = {
  idle: [0, 212, 255],
  scanning: [40, 160, 255],
  approved: [45, 255, 179],
  denied: [255, 59, 92],
  locked: [255, 176, 32],
  offline: [90, 110, 130],
};

const SPEED: Record<OrbMode, number> = {
  idle: 1,
  scanning: 2.8,
  approved: 1.3,
  denied: 0.7,
  locked: 0.35,
  offline: 0.2,
};

interface Particle {
  a: number;
  r: number;
  v: number;
  s: number;
  p: number;
}

interface Props {
  mode: OrbMode;
  /** 0..1 audio level; drives the waveform ring (microphone arrives in phase 4) */
  level?: number;
  className?: string;
}

export default function Orb({ mode, level = 0, className }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const modeRef = useRef(mode);
  const levelRef = useRef(level);
  const changedAt = useRef(performance.now());

  useEffect(() => {
    if (modeRef.current !== mode) changedAt.current = performance.now();
    modeRef.current = mode;
  }, [mode]);
  useEffect(() => {
    levelRef.current = level;
  }, [level]);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 0,
      h = 0,
      dpr = 1,
      raf = 0;
    const color = [...PALETTE[modeRef.current]];
    let speed = SPEED[modeRef.current];
    let phase = 0;
    let last = performance.now();

    const particles: Particle[] = Array.from({ length: 110 }, () => ({
      a: Math.random() * Math.PI * 2,
      r: 0.32 + Math.random() * 0.66,
      v: (0.05 + Math.random() * 0.25) * (Math.random() < 0.5 ? -1 : 1),
      s: 0.6 + Math.random() * 1.4,
      p: Math.random() * Math.PI * 2,
    }));

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      dpr = window.devicePixelRatio || 1;
      w = rect.width;
      h = rect.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    };
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();

    const rgba = (a: number) => `rgba(${color[0] | 0},${color[1] | 0},${color[2] | 0},${a})`;

    const arc = (r: number, a0: number, a1: number, lw: number, alpha: number, glow = 0) => {
      ctx.beginPath();
      ctx.arc(0, 0, r, a0, a1);
      ctx.lineWidth = lw;
      ctx.strokeStyle = rgba(alpha);
      ctx.shadowBlur = glow;
      ctx.shadowColor = rgba(0.9);
      ctx.stroke();
      ctx.shadowBlur = 0;
    };

    const draw = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const m = modeRef.current;
      const target = PALETTE[m];
      for (let i = 0; i < 3; i++) color[i] += (target[i] - color[i]) * 0.06;
      speed += (SPEED[m] - speed) * 0.04;
      phase += dt * speed * (reduced ? 0.15 : 1);
      const t = phase;
      const since = (now - changedAt.current) / 1000;
      const lvl = levelRef.current;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const R = (Math.min(w, h) / 2) * 0.94;
      ctx.translate(w / 2, h / 2);

      // denied: subtle glitch jitter
      if (m === "denied" && Math.random() < 0.12) {
        ctx.translate((Math.random() - 0.5) * 6, (Math.random() - 0.5) * 3);
      }

      // ambient glow
      const g = ctx.createRadialGradient(0, 0, R * 0.1, 0, 0, R);
      g.addColorStop(0, rgba(0.22));
      g.addColorStop(0.45, rgba(0.07));
      g.addColorStop(1, rgba(0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(0, 0, R, 0, Math.PI * 2);
      ctx.fill();

      // tick ring
      ctx.save();
      ctx.rotate(-t * 0.05);
      for (let i = 0; i < 120; i++) {
        const long = i % 5 === 0;
        const a = (i / 120) * Math.PI * 2;
        const r0 = R * (long ? 0.93 : 0.95);
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
        ctx.lineTo(Math.cos(a) * R * 0.975, Math.sin(a) * R * 0.975);
        ctx.lineWidth = long ? 1.4 : 0.8;
        ctx.strokeStyle = rgba(long ? 0.5 : 0.22);
        ctx.stroke();
      }
      ctx.restore();

      // ring A: hairline + two travelling arcs
      arc(R * 0.87, 0, Math.PI * 2, 1, 0.14);
      ctx.save();
      ctx.rotate(t * 0.35);
      arc(R * 0.87, 0, 0.95, 2.2, 0.85, 10);
      arc(R * 0.87, Math.PI, Math.PI + 0.4, 2.2, 0.6, 6);
      ctx.restore();

      // ring B: dashed, counter-rotating
      ctx.save();
      ctx.rotate(-t * 0.22);
      ctx.setLineDash([2, 7]);
      arc(R * 0.78, 0, Math.PI * 2, 2, 0.45);
      ctx.setLineDash([]);
      ctx.restore();

      // ring C: heavy segmented arcs
      ctx.save();
      ctx.rotate(t * 0.55);
      for (let i = 0; i < 3; i++) {
        const a0 = (i / 3) * Math.PI * 2;
        arc(R * 0.68, a0, a0 + 1.35, 5, 0.55, 14);
      }
      ctx.restore();
      ctx.save();
      ctx.rotate(-t * 0.8);
      for (let i = 0; i < 6; i++) {
        const a0 = (i / 6) * Math.PI * 2;
        arc(R * 0.62, a0, a0 + 0.35, 1.5, 0.5);
      }
      ctx.restore();

      // scanning sweep
      if (m === "scanning") {
        ctx.save();
        ctx.rotate(t * 1.3);
        for (let i = 0; i < 24; i++) {
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.arc(0, 0, R * 0.9, -i * 0.03, -(i + 1) * 0.03, true);
          ctx.closePath();
          ctx.fillStyle = rgba(0.16 * (1 - i / 24));
          ctx.fill();
        }
        ctx.restore();
      }

      // waveform ring (reacts to voice level)
      ctx.beginPath();
      const amp = R * (0.008 + lvl * 0.07) * (m === "scanning" ? 1.8 : 1);
      for (let i = 0; i <= 200; i++) {
        const a = (i / 200) * Math.PI * 2;
        const n =
          Math.sin(a * 6 + t * 2.1) * 0.6 + Math.sin(a * 13 - t * 3.3) * 0.3 + Math.sin(a * 23 + t * 5) * 0.2;
        const r = R * 0.5 + n * amp;
        const x = Math.cos(a) * r,
          y = Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = rgba(0.75);
      ctx.shadowBlur = 8;
      ctx.shadowColor = rgba(1);
      ctx.stroke();
      ctx.shadowBlur = 0;

      // particles
      for (const p of particles) {
        p.a += p.v * dt * speed;
        const rr = R * p.r * (1 + Math.sin(t * 0.7 + p.p) * 0.02);
        const tw = 0.35 + 0.65 * Math.abs(Math.sin(t * 1.5 + p.p));
        ctx.fillStyle = rgba(0.55 * tw);
        ctx.beginPath();
        ctx.arc(Math.cos(p.a) * rr, Math.sin(p.a) * rr, p.s, 0, Math.PI * 2);
        ctx.fill();
      }

      // core
      const pulse = 1 + Math.sin(t * 2.2) * 0.035 + lvl * 0.12;
      const cr = R * 0.27 * pulse;
      const cg = ctx.createRadialGradient(0, 0, 0, 0, 0, cr);
      cg.addColorStop(0, "rgba(255,255,255,0.95)");
      cg.addColorStop(0.25, rgba(0.9));
      cg.addColorStop(0.7, rgba(0.25));
      cg.addColorStop(1, rgba(0));
      ctx.fillStyle = cg;
      ctx.beginPath();
      ctx.arc(0, 0, cr, 0, Math.PI * 2);
      ctx.fill();
      arc(R * 0.34, 0, Math.PI * 2, 1.2, 0.5);

      // approval shockwave
      if (m === "approved" && since < 1.6) {
        const k = since / 1.6;
        arc(R * (0.34 + k * 0.66), 0, Math.PI * 2, 3 * (1 - k) + 0.5, 0.8 * (1 - k), 18);
      }

      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return <canvas ref={canvasRef} className={className} aria-hidden="true" />;
}
