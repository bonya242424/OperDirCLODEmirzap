import { useMemo } from "react";
import type { Theme } from "../theme";

// Собственная отрисовка: холмы и небо. Звёзды генерируются детерминированно,
// чтобы картинка не «мигала» при перерисовке.
function makeStars(count: number) {
  let seed = 7;
  const next = () => {
    seed = (seed * 16807) % 2147483647;
    return seed / 2147483647;
  };
  return Array.from({ length: count }, () => ({
    x: next() * 1600,
    y: next() * 470,
    r: 0.5 + next() * 1.2,
    o: 0.35 + next() * 0.65,
  }));
}

export default function Wallpaper({ theme }: { theme: Theme }) {
  const stars = useMemo(() => makeStars(110), []);
  const night = theme === "night";

  return (
    <svg className="wallpaper" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
      <defs>
        <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
          {night ? (
            <>
              <stop offset="0" stopColor="#050a1c" />
              <stop offset="0.6" stopColor="#102042" />
              <stop offset="1" stopColor="#1d3560" />
            </>
          ) : (
            <>
              <stop offset="0" stopColor="#a9c9e6" />
              <stop offset="0.55" stopColor="#efe0c8" />
              <stop offset="1" stopColor="#f7e6c3" />
            </>
          )}
        </linearGradient>
        <linearGradient id="hill-far" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={night ? "#16273f" : "#9bb263"} />
          <stop offset="1" stopColor={night ? "#0c1626" : "#7c9a47"} />
        </linearGradient>
        <linearGradient id="hill-mid" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={night ? "#0f1c2f" : "#7f9d44"} />
          <stop offset="1" stopColor={night ? "#09121f" : "#5f8330"} />
        </linearGradient>
        <linearGradient id="hill-near" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={night ? "#0a1422" : "#628a2f"} />
          <stop offset="1" stopColor={night ? "#060c15" : "#436a1e"} />
        </linearGradient>
        <radialGradient id="glow" cx="0.5" cy="1" r="0.8">
          <stop offset="0" stopColor={night ? "#27407a" : "#fff3d6"} stopOpacity={night ? 0.55 : 0.85} />
          <stop offset="1" stopColor={night ? "#27407a" : "#fff3d6"} stopOpacity="0" />
        </radialGradient>
      </defs>

      <rect width="1600" height="900" fill="url(#sky)" />
      <ellipse cx="800" cy="560" rx="900" ry="360" fill="url(#glow)" />
      {night &&
        stars.map((s, i) => <circle key={i} cx={s.x} cy={s.y} r={s.r} fill="#fff" opacity={s.o} />)}

      <path
        d="M0 560 C200 500 420 545 640 520 S1100 470 1300 520 S1500 545 1600 500 L1600 900 L0 900 Z"
        fill="url(#hill-far)"
      />
      <path d="M0 655 C250 585 500 645 800 612 S1250 560 1600 645 L1600 900 L0 900 Z" fill="url(#hill-mid)" />
      <path d="M0 765 C300 705 620 785 960 735 S1400 695 1600 755 L1600 900 L0 900 Z" fill="url(#hill-near)" />
    </svg>
  );
}
