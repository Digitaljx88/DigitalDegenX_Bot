"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type PortfolioResponse = {
  uid: number;
  portfolio: Record<string, number>;
  paper?: {
    sol_balance: number;
    sol_price_usd: number;
    total_value_sol: number;
    total_value_usd: number;
    error?: string;
    positions: Array<{
      mint: string;
      symbol: string;
      name: string;
      raw_amount: number;
      ui_amount: number;
      decimals: number;
      price_sol?: number | null;
      price_usd?: number | null;
      value_sol?: number | null;
      value_usd?: number | null;
      buy_price_usd?: number | null;
      pnl_pct?: number | null;
      mcap?: number | null;
      auto_sell_enabled: boolean;
      entry_sol?: number | null;
      next_target?: string | null;
      error?: string;
    }>;
  };
};

type WalletToken = {
  mint: string;
  amount: number;
  ui_amount?: number;
  symbol?: string;
};

type WalletResponse = {
  pubkey: string;
  sol: number;
  tokens: WalletToken[];
};

type ModeResponse = {
  uid: number;
  mode: "paper" | "live";
};

async function fetchPortfolioFor(uid: number) {
  return apiFetch<PortfolioResponse>("/portfolio", { query: { uid } });
}

export function PortfolioDashboard() {
  const { uid } = useActiveUid();
  const [portfolio, setPortfolio] = useState<Record<string, number>>({});
  const [paperView, setPaperView] = useState<PortfolioResponse["paper"] | null>(null);
  const [wallet, setWallet] = useState<WalletResponse | null>(null);
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [error, setError] = useState("");
  const [sellingMint, setSellingMint] = useState("");

  useEffect(() => {
    async function loadPortfolio() {
      if (!uid) {
        setPortfolio({});
        setPaperView(null);
        setWallet(null);
        return;
      }
      const activeUid = uid;
      try {
        const [portfolioRes, walletRes, modeRes] = await Promise.all([
          fetchPortfolioFor(activeUid),
          apiFetch<WalletResponse>("/wallet"),
          apiFetch<ModeResponse>("/mode", { query: { uid: activeUid } }),
        ]);
        setPortfolio(portfolioRes.portfolio || {});
        setPaperView(portfolioRes.paper || null);
        setWallet(walletRes);
        setMode(modeRes.mode || "paper");
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load portfolio");
      }
    }
    loadPortfolio();
  }, [uid]);

  async function quickSell(mint: string, pct: number) {
    const activeUid = uid;
    if (!activeUid) {
      setError("Set a Telegram UID before selling.");
      return;
    }
    setSellingMint(`${mint}:${pct}`);
    try {
      await apiFetch("/sell", {
        method: "POST",
        body: JSON.stringify({ uid: activeUid, mint, pct, mode: "paper" }),
      });
      const [portfolioRes, walletRes, modeRes] = await Promise.all([
        fetchPortfolioFor(activeUid),
        apiFetch<WalletResponse>("/wallet"),
        apiFetch<ModeResponse>("/mode", { query: { uid: activeUid } }),
      ]);
      setPortfolio(portfolioRes.portfolio || {});
      setPaperView(portfolioRes.paper || null);
      setWallet(walletRes);
      setMode(modeRes.mode || "paper");
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sell failed");
    } finally {
      setSellingMint("");
    }
  }

  if (!uid) {
    return (
      <Panel title="Portfolio" subtitle="Set your Telegram UID to load balances and quick sell controls.">
        <div className="text-sm text-[var(--muted-foreground)]">
          Add your Telegram UID in the top bar, then this page will load your paper balances and quick sell actions.
        </div>
      </Panel>
    );
  }

  const liveTokens = (wallet?.tokens || []).filter((token) => Number(token.ui_amount || token.amount || 0) > 0);
  const paperPositions = paperView?.positions || [];
  const paperSolBalance = Number(paperView?.sol_balance ?? portfolio.SOL ?? 0);

  function formatCompactUsd(value?: number | null) {
    const amount = Number(value || 0);
    if (!amount) return null;
    if (amount >= 1_000_000) return `$${(amount / 1_000_000).toFixed(2)}M`;
    if (amount >= 1_000) return `$${(amount / 1_000).toFixed(1)}K`;
    return `$${amount.toFixed(2)}`;
  }

  function formatPnl(value?: number | null) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return null;
    }
    const pnl = Number(value);
    return `${pnl >= 0 ? "+" : ""}${pnl.toFixed(1)}%`;
  }

  return (
    <div className="space-y-6">
      <Panel
        title="Portfolio"
        subtitle={`Telegram mode is currently ${mode === "paper" ? "Paper" : "Live"}. Telegram /portfolio follows that mode, while this page shows both paper balances and live wallet holdings.`}
      >
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        {paperView?.error ? (
          <div className="mb-4 rounded-2xl border border-amber-400/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            Paper portfolio enrichment hit an issue: {paperView.error}
          </div>
        ) : null}
        <div className="mb-4 rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-[var(--muted-foreground)]">
          Current mode: <span className="font-medium text-white">{mode === "paper" ? "Paper Portfolio" : "Live Wallet"}</span>
        </div>
        {paperView ? (
          <div className="mb-4 grid gap-3 md:grid-cols-3">
            <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-sm text-[var(--muted-foreground)]">Paper SOL</div>
              <div className="mt-2 text-2xl font-semibold text-white">{paperSolBalance.toLocaleString()}</div>
            </div>
            <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-sm text-[var(--muted-foreground)]">Paper Value</div>
              <div className="mt-2 text-2xl font-semibold text-white">
                {Number(paperView.total_value_sol || 0).toLocaleString(undefined, { maximumFractionDigits: 4 })} SOL
              </div>
              <div className="mt-2 text-xs text-[var(--muted-foreground)]">{formatCompactUsd(paperView.total_value_usd)}</div>
            </div>
            <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-sm text-[var(--muted-foreground)]">Tracked Tokens</div>
              <div className="mt-2 text-2xl font-semibold text-white">{paperPositions.length}</div>
            </div>
          </div>
        ) : null}
        <div className="mb-3 text-sm font-medium text-white">Paper Portfolio</div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {paperPositions.length > 0
            ? paperPositions.map((position) => (
                <div key={position.mint} className="rounded-2xl border border-white/8 bg-black/10 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm text-[var(--muted-foreground)]">{position.symbol}</div>
                      <div className="mt-1 text-lg font-semibold text-white">{position.name}</div>
                    </div>
                    {position.auto_sell_enabled ? (
                      <div className="rounded-full border border-emerald-400/20 bg-emerald-500/10 px-3 py-1 text-xs text-emerald-200">
                        Auto-sell on
                      </div>
                    ) : null}
                  </div>
                  <div className="mt-3 text-2xl font-semibold text-white">
                    {Number(position.ui_amount || 0).toLocaleString(undefined, { maximumFractionDigits: 4 })}
                  </div>
                  <div className="mt-2 text-xs text-[var(--muted-foreground)]">{position.mint}</div>
                  <div className="mt-3 flex flex-wrap gap-2 text-xs text-[var(--muted-foreground)]">
                    {position.value_sol ? <span>Value {position.value_sol.toFixed(4)} SOL</span> : null}
                    {position.value_usd ? <span>{formatCompactUsd(position.value_usd)}</span> : null}
                    {position.mcap ? <span>MCap {formatCompactUsd(position.mcap)}</span> : null}
                    {position.entry_sol ? <span>Entry {position.entry_sol.toFixed(3)} SOL</span> : null}
                    {position.next_target ? <span>Next {position.next_target}</span> : null}
                    {formatPnl(position.pnl_pct) ? <span>P&amp;L {formatPnl(position.pnl_pct)}</span> : null}
                    {position.error ? <span>Metadata fallback</span> : null}
                  </div>
                  <div className="mt-4 flex gap-2">
                    {[25, 50, 100].map((pct) => (
                      <button
                        key={pct}
                        type="button"
                        onClick={() => quickSell(position.mint, pct)}
                        disabled={sellingMint === `${position.mint}:${pct}`}
                        className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-[var(--muted-foreground)] disabled:opacity-50"
                      >
                        {sellingMint === `${position.mint}:${pct}` ? "..." : `Sell ${pct}%`}
                      </button>
                    ))}
                  </div>
                </div>
              ))
            : (
              <div className="rounded-2xl border border-white/8 bg-black/10 p-4 text-sm text-[var(--muted-foreground)]">
                No enriched paper positions available yet. If you recently deployed, refresh after the bot/API restart finishes.
              </div>
            )}
        </div>
        <div className="mt-6 mb-3 text-sm font-medium text-white">Live Wallet</div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
            <div className="text-sm text-[var(--muted-foreground)]">Wallet SOL</div>
            <div className="mt-2 text-2xl font-semibold text-white">{Number(wallet?.sol || 0).toLocaleString()}</div>
            {wallet?.pubkey ? <div className="mt-2 text-xs text-[var(--muted-foreground)]">{wallet.pubkey}</div> : null}
          </div>
          {liveTokens.map((token) => (
            <div key={token.mint} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-sm text-[var(--muted-foreground)]">{token.symbol || token.mint.slice(0, 8)}</div>
              <div className="mt-2 text-2xl font-semibold text-white">
                {Number(token.ui_amount || token.amount || 0).toLocaleString()}
              </div>
              <div className="mt-2 text-xs text-[var(--muted-foreground)]">{token.mint}</div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
