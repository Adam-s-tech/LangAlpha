import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

const PARTICLE_COUNT = 68;
const TRAIL_SPAN = 0.34;
const DURATION_MS = 6000;
const PULSE_DURATION_MS = 5400;
const AMP = 24;
const AMP_BOOST = 6;
const AX = 3;
const BY = 4;
const PHASE = 1.57;
const Y_SCALE = 0.92;
// Outlasts a caller's opacity fade, so the dots never freeze while still seen.
const SETTLE_MS = 250;

function normalizeProgress(p: number) {
  return ((p % 1) + 1) % 1;
}

function getDetailScale(time: number) {
  const pulseProgress = (time % PULSE_DURATION_MS) / PULSE_DURATION_MS;
  return 0.52 + ((Math.sin(pulseProgress * Math.PI * 2 + 0.55) + 1) / 2) * 0.48;
}

function point(progress: number, detailScale: number) {
  const t = progress * Math.PI * 2;
  const amp = AMP + detailScale * AMP_BOOST;
  return {
    x: 50 + Math.sin(AX * t + PHASE) * amp,
    y: 50 + Math.sin(BY * t) * amp * Y_SCALE,
  };
}

// Radius and opacity depend only on trail position, not time.
const RADIUS: number[] = [];
const ALPHA: number[] = [];
for (let i = 0; i < PARTICLE_COUNT; i++) {
  const fade = Math.pow(1 - i / (PARTICLE_COUNT - 1), 0.56);
  RADIUS.push(0.9 + fade * 2.7);
  ALPHA.push(0.04 + fade * 0.96);
}

interface LissajousLoadingProps {
  className?: string;
  /** False while the caller has faded the glyph out: the frame loop stops once
   *  the fade has run, instead of redrawing a figure nobody can see. */
  active?: boolean;
}

/**
 * Drawn on a canvas rather than as 68 SVG circles: moving the circles cost a
 * style recalc of the page every frame, which made the glyph more than twice
 * as expensive to animate. It keeps `currentColor` semantics by reading the
 * computed color when it starts and on every theme flip.
 */
export default function LissajousLoading({
  className,
  active = true,
}: LissajousLoadingProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const startRef = useRef<number>(0);

  const [running, setRunning] = useState(active);
  if (active && !running) setRunning(true);
  useEffect(() => {
    if (active || !running) return;
    const id = setTimeout(() => setRunning(false), SETTLE_MS);
    return () => clearTimeout(id);
  }, [active, running]);

  useEffect(() => {
    startRef.current = performance.now();
  }, []);

  // className is a dependency so a caller's new color class is read again.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = running ? canvas?.getContext("2d") : null;
    if (!canvas || !ctx) return;

    let color = getComputedStyle(canvas).color;
    let cssWidth = 0;
    let cssHeight = 0;

    function draw(now: number) {
      // Read per frame, not measured: moving the window to another display
      // changes the ratio without resizing the box.
      const dpr = window.devicePixelRatio || 1;
      const width = Math.round(cssWidth * dpr);
      const height = Math.round(cssHeight * dpr);
      if (!width || !height) return;
      if (canvas!.width !== width || canvas!.height !== height) {
        canvas!.width = width;
        canvas!.height = height;
      }

      const time = now - startRef.current;
      const progress = (time % DURATION_MS) / DURATION_MS;
      const detailScale = getDetailScale(time);

      ctx!.setTransform(width / 100, 0, 0, height / 100, 0, 0);
      ctx!.clearRect(0, 0, 100, 100);
      ctx!.fillStyle = color;
      for (let i = 0; i < PARTICLE_COUNT; i++) {
        const p = point(
          normalizeProgress(progress - (i / (PARTICLE_COUNT - 1)) * TRAIL_SPAN),
          detailScale,
        );
        ctx!.globalAlpha = ALPHA[i];
        ctx!.beginPath();
        ctx!.arc(p.x, p.y, RADIUS[i], 0, Math.PI * 2);
        ctx!.fill();
      }
    }

    let raf = 0;
    function frame(now: number) {
      draw(now);
      raf = requestAnimationFrame(frame);
    }

    // The first observation lands after layout and before paint, so a resumed
    // figure is placed where the running clock says before it is seen, never
    // shown frozen where it stopped.
    const sizes = new ResizeObserver(([entry]) => {
      cssWidth = entry.contentRect.width;
      cssHeight = entry.contentRect.height;
      draw(performance.now());
    });
    sizes.observe(canvas);
    const themes = new MutationObserver(() => {
      color = getComputedStyle(canvas).color;
    });
    themes.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "class"],
    });
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      sizes.disconnect();
      themes.disconnect();
    };
  }, [running, className]);

  return (
    <div className={cn("relative", className)}>
      <canvas ref={canvasRef} className="w-full h-full" aria-hidden="true" />
    </div>
  );
}
