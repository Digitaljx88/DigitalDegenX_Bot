"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type TickerItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  buy_ratio_5m?: number;
  alerted?: number;
  dq?: string;
};

function fmtMcap(v?: number) {
  if (!v) return "";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function scoreColor(item: TickerItem) {
  if (item.dq) return "text-red-400";
  const s = item.score ?? 0;
  if (s >= 70) return "text-emerald-400";
  if (s >= 50) return "text-amber-400";
  return "text-white/40";
}

export function TickerBar() {
  const [items, setItems] = useState<TickerItem[]>([]);

  useEffect(() => {
    async function load() {
      try {
        const data = await apiFetch<{ items: TickerItem[] }>("/scanner/feed", { query: { limit: 30 } });
        setItems(data.items || []);
      } catch {
        // silent — ticker is non-critical
      }
    }
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  if (!items.length) return null;

  // Double the list so the scroll loop is seamless
  const doubled = [...items, ...items];

  return (
    <div className="group relative overflow-hidden border-b border-white/6 bg-black/50 py-1.5">
      <div className="absolute left-0 top-0 bottom-0 z-10 flex items-center bg-gradient-to-r from-black/50 via-black/50 to-transparent pl-2 pr-4">
        <span className="text-[9px] font-semibold uppercase tracking-[0.2em] text-white/20">Live</span>
      </div>
      <div className="ticker-scroll group-hover:[animation-play-state:paused] flex gap-8 whitespace-nowrap px-4">
        {doubled.map((item, i) => (
          <Link
            key={`${item.mint}-${i}`}
            href={`/token/${item.mint}`}
            className="inline-flex items-center gap-1.5 text-[11px] hover:opacity-75"
          >
            <span className="font-semibold text-white">
              {item.symbol || item.name || item.mint.slice(0, 6)}
            </span>
            <span className={`font-mono tabular-nums ${scoreColor(item)}`}>
              {item.score ?? 0}
            </span>
            {item.mcap ? (
              <span className="text-white/35">{fmtMcap(item.mcap)}</span>
            ) : null}
            {item.buy_ratio_5m != null ? (
              <span className={item.buy_ratio_5m >= 0.6 ? "text-emerald-400/60" : "text-white/20"}>
                {Math.round(item.buy_ratio_5m * 100)}%
              </span>
            ) : null}
            <span className="text-white/10">│</span>
          </Link>
        ))}
      </div>
    </div>
  );
}
