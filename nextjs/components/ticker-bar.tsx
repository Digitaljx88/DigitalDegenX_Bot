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
  alerted?: number;
  dq?: string;
};

function fmtMcap(v?: number) {
  if (!v) return "";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function scoreColor(score: number, dq?: string): string {
  if (dq) return "var(--red)";
  if (score >= 65) return "var(--accent)";
  if (score >= 50) return "var(--yellow)";
  return "var(--text2)";
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

  const doubled = [...items, ...items];

  return (
    <div
      className="overflow-hidden flex items-center"
      style={{
        background: "var(--bg1)",
        borderBottom: "1px solid var(--border)",
        height: 32,
      }}
    >
      <div className="ticker-scroll flex whitespace-nowrap">
        {doubled.map((item, i) => {
          const score = item.score ?? 0;
          const mcap = fmtMcap(item.mcap);
          return (
            <Link
              key={`${item.mint}-${i}`}
              href={`/token/${item.mint}`}
              className="inline-flex items-center gap-1.5 hover:bg-white/[0.04]"
              style={{
                padding: "0 20px",
                fontSize: 11,
                fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                borderRight: "1px solid var(--border)",
              }}
            >
              <span style={{ color: "var(--text2)", fontWeight: 500, letterSpacing: "0.03em" }}>
                {item.symbol || item.name || item.mint.slice(0, 6)}
              </span>
              <span style={{ color: scoreColor(score, item.dq), fontWeight: 600 }}>
                {score}
              </span>
              {mcap ? (
                <span style={{ color: "var(--text3)" }}>{mcap}</span>
              ) : null}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
