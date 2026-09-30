import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

const SVG_NS = "http://www.w3.org/2000/svg";

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

interface LissajousLoadingProps {
  className?: string;
  /** False while the caller has faded the glyph out: the frame loop stops once
   *  the fade has run, instead of redrawing 68 dots nobody can see. */
  active?: boolean;
}

export default function LissajousLoading({
  className,
  active = true,
}: LissajousLoadingProps) {
  const groupRef = useRef<SVGGElement>(null);
  const particlesRef = useRef<SVGCircleElement[]>([]);
  const startRef = useRef<number>(0);

  const [running, setRunning] = useState(active);
  if (active && !running) setRunning(true);
  useEffect(() => {
    if (active || !running) return;
    const id = setTimeout(() => setRunning(false), SETTLE_MS);
    return () => clearTimeout(id);
  }, [active, running]);

  useEffect(() => {
    const group = groupRef.current;
    if (!group) return;

    const particles: SVGCircleElement[] = [];
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("fill", "currentColor");

      // Radius and opacity depend only on trail position (index), not time.
      // Set once to avoid per-frame setAttribute overhead.
      const fade = Math.pow(1 - i / (PARTICLE_COUNT - 1), 0.56);
      circle.setAttribute("r", (0.9 + fade * 2.7).toFixed(2));
      circle.setAttribute("opacity", (0.04 + fade * 0.96).toFixed(3));

      group.appendChild(circle);
      particles.push(circle);
    }
    particlesRef.current = particles;
    startRef.current = performance.now();

    return () => {
      particles.forEach((c) => c.remove());
      particlesRef.current = [];
    };
  }, []);

  useEffect(() => {
    const particles = particlesRef.current;
    if (!running || !particles.length) return;

    let raf = 0;
    function render(now: number) {
      const time = now - startRef.current;
      const progress = (time % DURATION_MS) / DURATION_MS;
      const detailScale = getDetailScale(time);

      for (let i = 0; i < PARTICLE_COUNT; i++) {
        const p = point(
          normalizeProgress(progress - (i / (PARTICLE_COUNT - 1)) * TRAIL_SPAN),
          detailScale,
        );
        particles[i].setAttribute("cx", p.x.toFixed(2));
        particles[i].setAttribute("cy", p.y.toFixed(2));
      }

      raf = requestAnimationFrame(render);
    }

    // The clock kept running through a pause, so the figure resumes where it
    // would have been, and is placed before the first frame rather than on it.
    render(performance.now());
    return () => cancelAnimationFrame(raf);
  }, [running]);

  return (
    <div className={cn("relative", className)}>
      <svg
        viewBox="0 0 100 100"
        fill="none"
        className="w-full h-full overflow-visible"
        aria-hidden="true"
      >
        <g ref={groupRef} />
      </svg>
    </div>
  );
}
