"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type PortfolioResponse = {
  uid: number;
  portfolio: Record<string, number>;
};

async function fetchPortfolioFor(uid: number) {
  return apiFetch<PortfolioResponse>("/portfolio", { query: { uid } });
}

export function PortfolioDashboard() {
  const searchParams = useSearchParams();
  const uid = Number(searchParams.get("uid") || 0);
  const [portfolio, setPortfolio] = useState<Record<string, number>>({});
  const [error, setError] = useState("");
  const [sellingMint, setSellingMint] = useState("");

  useEffect(() => {
    async function loadPortfolio() {
      if (!uid) {
        setPortfolio({});
        return;
      }
      try {
        const data = await fetchPortfolioFor(uid);
        setPortfolio(data.portfolio || {});
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load portfolio");
      }
    }
    loadPortfolio();
  }, [uid]);

  async function quickSell(mint: string, pct: number) {
    setSellingMint(`${mint}:${pct}`);
    try {
      await apiFetch("/sell", {
        method: "POST",
        body: JSON.stringify({ uid, mint, pct, mode: "paper" }),
      });
      const data = await fetchPortfolioFor(uid);
      setPortfolio(data.portfolio || {});
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

  return (
    <div className="space-y-6">
      <Panel title="Portfolio" subtitle="Current paper balances with quick sell controls for held tokens.">
        {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
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
      </Panel>
    </div>
  );
}
