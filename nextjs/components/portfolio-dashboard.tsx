"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";
import { useActiveUid } from "@/lib/active-uid";

type PortfolioResponse = {
  uid: number;
  portfolio: Record<string, number>;
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
  const [wallet, setWallet] = useState<WalletResponse | null>(null);
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [error, setError] = useState("");
  const [sellingMint, setSellingMint] = useState("");

  useEffect(() => {
    async function loadPortfolio() {
      if (!uid) {
        setPortfolio({});
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

  const entries = Object.entries(portfolio || {});
  const liveTokens = (wallet?.tokens || []).filter((token) => Number(token.ui_amount || token.amount || 0) > 0);

  return (
    <div className="space-y-6">
      <Panel
        title="Portfolio"
        subtitle={`Telegram mode is currently ${mode === "paper" ? "Paper" : "Live"}. Telegram /portfolio follows that mode, while this page shows both paper balances and live wallet holdings.`}
      >
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
        <div className="mb-4 rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-[var(--muted-foreground)]">
          Current mode: <span className="font-medium text-white">{mode === "paper" ? "Paper Portfolio" : "Live Wallet"}</span>
        </div>
        <div className="mb-3 text-sm font-medium text-white">Paper Portfolio</div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {entries.map(([asset, amount]) => (
            <div key={asset} className="rounded-2xl border border-white/8 bg-black/10 p-4">
              <div className="text-sm text-[var(--muted-foreground)]">{asset === "SOL" ? "Solana" : asset}</div>
              <div className="mt-2 text-2xl font-semibold text-white">{Number(amount).toLocaleString()}</div>
              {asset !== "SOL" ? (
                <div className="mt-4 flex gap-2">
                  {[25, 50, 100].map((pct) => (
                    <button
                      key={pct}
                      type="button"
                      onClick={() => quickSell(asset, pct)}
                      disabled={sellingMint === `${asset}:${pct}`}
                      className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-[var(--muted-foreground)] disabled:opacity-50"
                    >
                      {sellingMint === `${asset}:${pct}` ? "..." : `Sell ${pct}%`}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
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
