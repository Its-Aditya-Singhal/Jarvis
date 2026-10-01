import { useEffect, useRef } from "react";
import { getMicLevel } from "../lib/store";

/** Scrolling, mirrored level bars driven by the backend microphone level. */
export default function Waveform({ active, className = "" }: { active: boolean; className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const activeRef = useRef(active);
  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  useEffect(() => {
    const canvas = ref.current!;
    const ctx = canvas.getContext("2d")!;
    const history = new Array(72).fill(0);
    let raf = 0;
    let last = 0;
    let smooth = 0;

    const draw = (now: number) => {
      raf = requestAnimationFrame(draw);
      // ~30 frames a second is plenty for level bars, and nothing at all while the window is hidden
      if (document.hidden || now - last < 33) return;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(rect.width * dpr)) {
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
      }
      smooth += (getMicLevel() - smooth) * 0.5;
      last = now;
      history.push(smooth);
      history.shift();
      const w = rect.width;
      const h = rect.height;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const n = history.length;
      const gap = 3;
      const bw = (w - gap * (n - 1)) / n;
      const hot = activeRef.current;
      history.forEach((v, i) => {
        const bh = Math.max(2, v * h * 0.92);
        const age = i / n;
        ctx.fillStyle = `rgba(76,157,255,${(0.2 + 0.8 * age) * (hot ? 1 : 0.5)})`;
        ctx.fillRect(i * (bw + gap), (h - bh) / 2, bw, bh);
      });
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={ref} className={`waveform ${className}`} aria-hidden="true" />;
}
