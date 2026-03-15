"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";
import { Tooltip } from "@/components/tooltip";
import { ExternalLinks } from "@/components/external-links";

type ScannerFeedItem = {
  mint: string;
  symbol?: string;
  name?: string;
  score?: number;
  mcap?: number;
  narrative?: string;
  source_primary?: string;
  strategy_profile?: string;
  state?: string;
  confidence?: number;
  age_mins?: number;
  buy_ratio_5m?: number;
  alerted?: number;
  dq?: string;
  ts?: number;
  autobuy_preview?: {
    eligible?: boolean;
    status?: string;
    block_reason?: string;
    block_category?: string;
    sol_amount?: number;
    confidence?: number;
    strategy_profile?: string;
  };
};

type ScannerFeedResponse = { count: number; items: ScannerFeedItem[] };
type ModeResponse = { uid: number; mode: "paper" | "live" };

type FilterTab = "all" | "alerted" | "tracked" | "dq";

function fmtMcap(v?: number) {
  if (!v) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtAge(v?: number) {
  if (!v || v < 0) return "new";
  if (v < 1) return "<1m";
  if (v >= 60) return `${(v / 60).toFixed(1)}h`;
  return `${Math.round(v)}m`;
}

function scoreColor(score: number, dq?: string) {
  if (dq) return "text-red-400";
  if (score >= 75) return "text-emerald-400";
  if (score >= 60) return "text-amber-400";
  if (score >= 40) return "text-white/70";
  return "text-white/35";
}

function scoreBg(score: number, dq?: string) {
  if (dq) return "bg-red-500/20";
  if (score >= 75) return "bg-emerald-500/20";
  if (score >= 60) return "bg-amber-500/20";
  return "bg-white/8";
}

function buyRatioColor(r?: number) {
  if (r == null) return "text-white/30";
  if (r >= 0.65) return "text-emerald-400";
  if (r >= 0.5) return "text-amber-400";
  return "text-red-400/70";
}

export function ScannerDashboard() {
  const { uid } = useActiveUid();
  const [feed, setFeed] = useState<ScannerFeedItem[]>([]);
  const [lastUpdated, setLastUpdated] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [message, setMessage] = useState<string>("");
  const [submittingMint, setSubmittingMint] = useState<string>("");
  const [buyAmount, setBuyAmount] = useState("0.1");
  const [tradeMode, setTradeMode] = useState<"paper" | "live">("paper");
  const [tab, setTab] = useState<FilterTab>("all");

  const loadFeed = useCallback(async () => {
    try {
      const data = await apiFetch<ScannerFeedResponse>("/scanner/feed", {
        query: { limit: 40, uid: uid || undefined },
      });
      setFeed(data.items || []);
      setLastUpdated(new Date().toLocaleTimeString());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Feed error");
    }
  }, [uid]);

  useEffect(() => {
    loadFeed();
    const t = window.setInterval(() => loadFeed(), 5000);
    return () => window.clearInterval(t);
  }, [loadFeed]);

  useEffect(() => {
    async function loadMode() {
      if (!uid) { setTradeMode("paper"); return; }
      try {
        const d = await apiFetch<ModeResponse>("/mode", { query: { uid } });
        setTradeMode(d.mode || "paper");
      } catch { setTradeMode("paper"); }
    }
    void loadMode();
  }, [uid]);

  const filtered = useMemo(() => {
    const base = feed.slice(0, 40);
    if (tab === "alerted") return base.filter((i) => i.alerted && !i.dq);
    if (tab === "tracked") return base.filter((i) => !i.alerted && !i.dq);
    if (tab === "dq") return base.filter((i) => !!i.dq);
    return base;
  }, [feed, tab]);

  const counts = useMemo(() => ({
    all: feed.length,
    alerted: feed.filter((i) => i.alerted && !i.dq).length,
    tracked: feed.filter((i) => !i.alerted && !i.dq).length,
    dq: feed.filter((i) => !!i.dq).length,
  }), [feed]);

  async function quickBuy(mint: string) {
    if (!uid) { setError("Set your Telegram UID first."); return; }
    setSubmittingMint(mint);
    try {
      await apiFetch("/buy", {
        method: "POST",
        body: JSON.stringify({ uid, mint, sol_amount: Number(buyAmount || 0), mode: tradeMode }),
      });
      setError("");
      setMessage(`${tradeMode === "paper" ? "Paper" : "Live"} buy submitted.`);
      setTimeout(() => setMessage(""), 4000);
      void loadFeed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Buy failed");
      setTimeout(() => setError(""), 6000);
      setMessage("");
    } finally {
      setSubmittingMint("");
    }
  }

  const TABS: { key: FilterTab; label: string }[] = [
    { key: "all", label: "All" },
    { key: "alerted", label: "Alerted" },
    { key: "tracked", label: "Tracked" },
    { key: "dq", label: "DQ'd" },
  ];

  return (
    <div className="overflow-hidden rounded-[28px] border border-white/8 bg-[#080e14]">
      {/* Terminal header */}
      <div className="flex flex-col gap-3 border-b border-white/8 px-4 py-3 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="live-dot h-1.5 w-1.5 rounded-full bg-emerald-400" />
            <span className="text-[11px] font-semibold uppercase tracking-[0.2em] text-white/50">
              Scanner
            </span>
            {lastUpdated && (
              <span className="text-[10px] text-white/25">{lastUpdated}</span>
            )}
          </div>
          {/* Filter tabs */}
          <div className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className={`rounded px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider transition-colors ${
                  tab === t.key
                    ? "bg-white/12 text-white"
                    : "text-white/30 hover:text-white/60"
                }`}
              >
                {t.label}
                <span className={`ml-1 ${t.key === "dq" ? "text-red-400/60" : "text-white/25"}`}>{counts[t.key]}</span>
              </button>
            ))}
          </div>
        </div>
        {/* Buy controls */}
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {["0.05", "0.1", "0.25", "0.5"].map((amt) => (
              <button
                key={amt}
                type="button"
                onClick={() => setBuyAmount(amt)}
                className={`rounded px-2 py-1 text-[10px] font-mono ${
                  buyAmount === amt
                    ? "bg-[var(--accent)]/20 text-[var(--accent)]"
                    : "bg-white/5 text-white/40 hover:bg-white/10 hover:text-white/70"
                }`}
              >
                {amt}
              </button>
            ))}
            <input
              value={buyAmount}
              onChange={(e) => setBuyAmount(e.target.value)}
              className="w-14 rounded border border-white/10 bg-black/30 px-2 py-1 text-center text-[10px] font-mono text-white outline-none"
            />
            <span className="self-center text-[10px] text-white/30">SOL</span>
          </div>
          <button
            type="button"
            onClick={() => setTradeMode(tradeMode === "paper" ? "live" : "paper")}
            className={`rounded px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider ${
              tradeMode === "live"
                ? "bg-red-500/20 text-red-300"
                : "bg-white/8 text-white/50"
            }`}
          >
            {tradeMode === "paper" ? "Paper" : "Live"}
          </button>
        </div>
      </div>

      {/* Alerts */}
      {error && (
        <div className="border-b border-red-500/20 bg-red-500/10 px-4 py-2 text-xs text-red-300">
          {error}
        </div>
      )}
      {message && (
        <div className="border-b border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-xs text-emerald-300">
          {message}
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="min-w-full text-xs">
          <thead>
            <tr className="border-b border-white/6 text-left text-[10px] uppercase tracking-[0.18em] text-white/30">
              <th className="px-4 py-2">Token</th>
              <th className="px-3 py-2">
                Score
                <Tooltip text="Heat score 0–100. ≥75 = strong, ≥60 = alerted, <40 = weak." />
              </th>
              <th className="px-3 py-2">MCap</th>
              <th className="px-3 py-2">Age</th>
              <th className="px-3 py-2">
                Setup
                <Tooltip text="Narrative · strategy profile · confidence · 5m buy ratio" />
              </th>
              <th className="px-3 py-2">
                Status
                <Tooltip text="Alerted = Telegram alert sent. Tracked = below threshold. DQ = failed quality check." />
              </th>
              {uid ? (
                <th className="px-3 py-2">
                  Auto-Buy
                  <Tooltip text="Would the bot buy this right now given your config?" />
                </th>
              ) : null}
              <th className="px-3 py-2">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/4">
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={uid ? 8 : 7} className="px-4 py-10 text-center text-white/30">
                  {feed.length === 0
                    ? "Waiting for scanner feed — refreshing every 5s…"
                    : "No tokens match this filter."}
                </td>
              </tr>
            ) : null}
            {filtered.map((item) => {
              const score = item.score ?? 0;
              const isAlerted = !!item.alerted && !item.dq;
              const isDq = !!item.dq;
              return (
                <tr
                  key={`${item.mint}-${item.ts}`}
                  className="group hover:bg-white/[0.025]"
                >
                  {/* Token */}
                  <td className="px-4 py-2.5">
                    <div className="font-semibold text-white">
                      <Link
                        href={`/token/${item.mint}`}
                        className="hover:text-[var(--accent)]"
                      >
                        {item.symbol || item.name || item.mint.slice(0, 6)}
                      </Link>
                    </div>
                    <div className="mt-0.5 flex gap-1.5 text-[10px] text-white/25 uppercase tracking-wider">
                      <span>{item.source_primary || "scan"}</span>
                      {item.state ? <span>· {item.state.replaceAll("_", " ")}</span> : null}
                    </div>
                    <div className="mt-0.5 font-mono text-[9px] text-white/15">
                      {item.mint.slice(0, 12)}…
                    </div>
                    <ExternalLinks mint={item.mint} className="mt-1" />
                  </td>

                  {/* Score */}
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-1.5">
                      <span
                        className={`rounded px-1.5 py-0.5 font-mono text-sm font-bold tabular-nums ${scoreBg(score, item.dq)} ${scoreColor(score, item.dq)}`}
                      >
                        {score}
                      </span>
                    </div>
                    {/* Mini score bar */}
                    <div className="mt-1 h-0.5 w-12 overflow-hidden rounded-full bg-white/8">
                      <div
                        className={`h-full rounded-full ${isDq ? "bg-red-500" : score >= 75 ? "bg-emerald-500" : score >= 60 ? "bg-amber-500" : "bg-white/30"}`}
                        style={{ width: `${Math.min(100, score)}%` }}
                      />
                    </div>
                  </td>

                  {/* MCap */}
                  <td className="px-3 py-2.5 font-mono tabular-nums text-white/80">
                    {fmtMcap(item.mcap)}
                  </td>

                  {/* Age */}
                  <td className="px-3 py-2.5 text-white/50">
                    {fmtAge(item.age_mins)}
                  </td>

                  {/* Setup */}
                  <td className="px-3 py-2.5">
                    <div className="text-white/80">
                      {item.narrative || "Other"}
                    </div>
                    <div className="mt-0.5 text-[10px] text-white/40">
                      {item.strategy_profile || "unprofiled"}
                    </div>
                    <div className="mt-0.5 flex gap-2 text-[10px]">
                      <span className="text-white/30">
                        conf{" "}
                        <span className="text-white/60">
                          {item.confidence?.toFixed(2) ?? "0.00"}
                        </span>
                      </span>
                      {item.buy_ratio_5m != null ? (
                        <span className={buyRatioColor(item.buy_ratio_5m)}>
                          {Math.round(item.buy_ratio_5m * 100)}% buy
                        </span>
                      ) : null}
                    </div>
                  </td>

                  {/* Status */}
                  <td className="px-3 py-2.5">
                    {isDq ? (
                      <div>
                        <div className="flex items-center gap-1.5">
                          <span className="h-1.5 w-1.5 rounded-full bg-red-400" />
                          <span className="text-red-300">DQ'd</span>
                        </div>
                        {item.dq && (
                          <div className="mt-0.5 max-w-[140px] text-[10px] text-red-400/60 leading-tight">
                            {item.dq.replaceAll("_", " ")}
                          </div>
                        )}
                      </div>
                    ) : isAlerted ? (
                      <div className="flex items-center gap-1.5">
                        <span className="live-dot h-1.5 w-1.5 rounded-full bg-emerald-400" />
                        <span className="text-emerald-300">Alerted</span>
                      </div>
                    ) : (
                      <div className="flex items-center gap-1.5">
                        <span className="h-1.5 w-1.5 rounded-full bg-amber-400/60" />
                        <span className="text-amber-200/60">Tracked</span>
                      </div>
                    )}
                  </td>

                  {/* Auto-Buy preview */}
                  {uid ? (
                    <td className="px-3 py-2.5">
                      {item.autobuy_preview ? (
                        <div>
                          <div className={`flex items-center gap-1.5 ${item.autobuy_preview.eligible ? "text-emerald-300" : "text-white/40"}`}>
                            <span className={`h-1.5 w-1.5 rounded-full ${item.autobuy_preview.eligible ? "bg-emerald-400" : "bg-white/20"}`} />
                            {item.autobuy_preview.eligible ? "Would buy" : "Blocked"}
                          </div>
                          {item.autobuy_preview.eligible && (
                            <div className="mt-0.5 font-mono text-[10px] text-white/40">
                              {Number(item.autobuy_preview.sol_amount || 0).toFixed(3)} SOL
                            </div>
                          )}
                          {!item.autobuy_preview.eligible && item.autobuy_preview.block_category && (
                            <div className="mt-0.5 text-[10px] text-white/30">
                              {item.autobuy_preview.block_category.replaceAll("_", " ")}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span className="text-white/20">—</span>
                      )}
                    </td>
                  ) : null}

                  {/* Actions */}
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-1.5">
                      <Link
                        href={`/token/${item.mint}`}
                        className="rounded border border-white/10 px-2.5 py-1 text-[10px] text-white/50 hover:border-white/25 hover:text-white/80"
                      >
                        View
                      </Link>
                      <button
                        type="button"
                        onClick={() => quickBuy(item.mint)}
                        disabled={!uid || !!item.dq || submittingMint === item.mint}
                        className={`rounded px-2.5 py-1 text-[10px] font-medium disabled:opacity-30 ${
                          tradeMode === "live"
                            ? "bg-red-500/20 text-red-200 hover:bg-red-500/30"
                            : "bg-[var(--accent)]/15 text-[var(--accent)] hover:bg-[var(--accent)]/25"
                        }`}
                      >
                        {submittingMint === item.mint
                          ? "…"
                          : tradeMode === "paper"
                            ? "Paper"
                            : "Buy"}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
