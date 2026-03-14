"use client";

import Link from "next/link";
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
      narrative?: string | null;
      strategy_profile?: string | null;
      exit_profile?: string | null;
      purchase_timestamp?: number | null;
      target_count?: number;
      stop_loss_enabled?: boolean;
      trailing_stop_enabled?: boolean;
      first_risk_off_enabled?: boolean;
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

type AutoSellTarget = {
  mult: number;
  sell_pct: number;
  triggered?: boolean;
  label?: string;
};

type AutoSellConfig = {
  enabled?: boolean;
  narrative?: string;
  strategy_profile?: string;
  exit_profile?: string;
  buy_price_usd?: number;
  sol_amount?: number;
  purchase_timestamp?: number;
  mult_targets?: AutoSellTarget[];
  stop_loss?: {
    enabled?: boolean;
    pct?: number;
    sell_pct?: number;
  };
  trailing_stop?: {
    enabled?: boolean;
    trail_pct?: number;
    sell_pct?: number;
    post_partial_trail_pct?: number;
  };
  trailing_tp?: {
    enabled?: boolean;
    activate_mult?: number;
    trail_pct?: number;
    sell_pct?: number;
  };
  time_exit?: {
    enabled?: boolean;
    hours?: number;
    target_mult?: number;
    sell_pct?: number;
  };
  breakeven_stop?: {
    enabled?: boolean;
    activate_mult?: number;
  };
  first_risk_off?: {
    enabled?: boolean;
    activate_mult?: number;
    sell_pct?: number;
    tighten_trailing?: boolean;
    tighten_to_pct?: number;
  };
  velocity_rollover?: {
    enabled?: boolean;
    activate_mult?: number;
    sell_pct?: number;
    min_score_drop?: number;
    min_velocity?: number;
  };
};

type AutoSellResponse = {
  uid: number;
  mint: string;
  config: AutoSellConfig;
};

type PositionSnapshotResponse = {
  mint: string;
  analysis?: {
    effective_score?: number;
    raw_score?: number;
    risk?: string;
    matched_narrative?: string;
    strategy_profile?: string;
    strategy_exit_preset?: string;
    strategy_confidence?: number;
    archetype?: string;
    archetype_label?: string;
    breakdown?: Record<string, [number, string]>;
    entry_quality?: {
      age_band?: string;
      buy_ratio_5m?: number;
      liquidity_drop_pct?: number;
      score_slope?: number;
      holder_concentration_pct?: number;
    };
    quality_flags?: {
      alert_blocked?: boolean;
      autobuy_blocked?: boolean;
      force_scouted?: boolean;
      quality_reasons?: string[];
      force_scouted_reasons?: string[];
      autobuy_only_reasons?: string[];
    };
  } | null;
  lifecycle?: {
    state?: string;
    source_primary?: string;
    narrative?: string;
    archetype?: string;
    strategy_profile?: string;
    last_effective_score?: number;
    launch_ts?: number;
    migration_ts?: number;
  };
  metrics?: {
    buy_ratio_5m?: number;
    liquidity_usd?: number;
    liquidity_delta_pct?: number;
    holder_concentration?: number;
    score_slope?: number;
    score_acceleration?: number;
    peak_score?: number;
    time_since_peak_s?: number;
  };
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
  const [message, setMessage] = useState("");
  const [sellingMint, setSellingMint] = useState("");
  const [expandedMint, setExpandedMint] = useState<string | null>(null);
  const [autoSellByMint, setAutoSellByMint] = useState<Record<string, AutoSellConfig>>({});
  const [loadingAutoSellMint, setLoadingAutoSellMint] = useState("");
  const [savingAutoSellMint, setSavingAutoSellMint] = useState("");
  const [positionSnapshots, setPositionSnapshots] = useState<Record<string, PositionSnapshotResponse>>({});
  const [loadingSnapshotMint, setLoadingSnapshotMint] = useState("");

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
        setMessage("");
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
      setMessage(`Sold ${pct}% of ${mint.slice(0, 8)} from paper portfolio.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sell failed");
    } finally {
      setSellingMint("");
    }
  }

  async function openAutoSellEditor(mint: string) {
    if (expandedMint === mint) {
      setExpandedMint(null);
      return;
    }
    setExpandedMint(mint);
    const tasks: Promise<void>[] = [];
    if (!autoSellByMint[mint]) {
      setLoadingAutoSellMint(mint);
      tasks.push(
        apiFetch<AutoSellResponse>(`/autosell/${mint}`)
          .then((response) => {
            setAutoSellByMint((current) => ({ ...current, [mint]: response.config || {} }));
          })
          .finally(() => setLoadingAutoSellMint("")),
      );
    }
    if (!positionSnapshots[mint]) {
      setLoadingSnapshotMint(mint);
      tasks.push(
        apiFetch<PositionSnapshotResponse>(`/token/${mint}/snapshot`)
          .then((response) => {
            setPositionSnapshots((current) => ({ ...current, [mint]: response }));
          })
          .finally(() => setLoadingSnapshotMint("")),
      );
    }
    try {
      await Promise.all(tasks);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load position details");
    }
  }

  function updateAutoSellConfig(mint: string, updater: (current: AutoSellConfig) => AutoSellConfig) {
    setAutoSellByMint((current) => ({
      ...current,
      [mint]: updater(current[mint] || {}),
    }));
  }

  async function saveAutoSellConfig(mint: string) {
    const config = autoSellByMint[mint];
    if (!config) {
      return;
    }
    setSavingAutoSellMint(mint);
    try {
      await apiFetch(`/autosell/${mint}`, {
        method: "POST",
        body: JSON.stringify({
          enabled: config.enabled ?? true,
          mult_targets: (config.mult_targets || []).map((target) => ({
            mult: Number(target.mult || 0),
            sell_pct: Number(target.sell_pct || 0),
            label: target.label || `${Number(target.mult || 0)}x`,
          })),
          stop_loss: config.stop_loss,
          trailing_stop: config.trailing_stop,
          trailing_tp: config.trailing_tp,
          time_exit: config.time_exit,
          breakeven_stop: config.breakeven_stop,
          first_risk_off: config.first_risk_off,
          velocity_rollover: config.velocity_rollover,
        }),
      });
      const portfolioRes = await fetchPortfolioFor(uid!);
      setPaperView(portfolioRes.paper || null);
      setPortfolio(portfolioRes.portfolio || {});
      setMessage(`Saved auto-sell config for ${mint.slice(0, 8)}.`);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save auto-sell config");
    } finally {
      setSavingAutoSellMint("");
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
        {message ? <div className="mb-4 rounded-2xl border border-emerald-400/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{message}</div> : null}
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
                    {position.strategy_profile ? <span>Strategy {position.strategy_profile}</span> : null}
                    {position.exit_profile ? <span>Exit {position.exit_profile}</span> : null}
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
                    <button
                      type="button"
                      onClick={() => openAutoSellEditor(position.mint)}
                      className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-[var(--muted-foreground)]"
                    >
                      {expandedMint === position.mint ? "Hide exits" : "Manage exits"}
                    </button>
                  </div>
                  {expandedMint === position.mint ? (
                    <div className="mt-4 rounded-2xl border border-white/8 bg-black/20 p-4">
                      {loadingSnapshotMint === position.mint ? (
                        <div className="mb-4 text-sm text-[var(--muted-foreground)]">Loading position intel...</div>
                      ) : positionSnapshots[position.mint] ? (
                        <PositionIntel
                          mint={position.mint}
                          snapshot={positionSnapshots[position.mint]}
                          position={position}
                        />
                      ) : null}
                      {loadingAutoSellMint === position.mint ? (
                        <div className="text-sm text-[var(--muted-foreground)]">Loading exit config...</div>
                      ) : (
                        <>
                          {autoSellByMint[position.mint] ? (
                            <AutoSellEditor
                              position={position}
                              config={autoSellByMint[position.mint]}
                              saving={savingAutoSellMint === position.mint}
                              onChange={(updater) => updateAutoSellConfig(position.mint, updater)}
                              onSave={() => saveAutoSellConfig(position.mint)}
                            />
                          ) : (
                            <div className="text-sm text-[var(--muted-foreground)]">
                              No auto-sell config found for this position yet.
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  ) : null}
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

function numberOrZero(value?: number | null) {
  return Number(value || 0);
}

function formatTimestamp(value?: number | null) {
  if (!value) return "n/a";
  try {
    return new Date(Number(value) * 1000).toLocaleString();
  } catch {
    return "n/a";
  }
}

function AutoSellEditor({
  position,
  config,
  saving,
  onChange,
  onSave,
}: {
  position: NonNullable<PortfolioResponse["paper"]>["positions"][number];
  config: AutoSellConfig;
  saving: boolean;
  onChange: (updater: (current: AutoSellConfig) => AutoSellConfig) => void;
  onSave: () => void;
}) {
  const targets = config.mult_targets || [];

  function updateTarget(index: number, key: keyof AutoSellTarget, value: number | string) {
    onChange((current) => {
      const nextTargets = [...(current.mult_targets || [])];
      const existing = nextTargets[index] || { mult: 2, sell_pct: 50, label: "2x" };
      const updated = { ...existing, [key]: value };
      if (key === "mult") {
        updated.label = `${Number(value || 0)}x`;
      }
      nextTargets[index] = updated;
      return { ...current, mult_targets: nextTargets };
    });
  }

  function removeTarget(index: number) {
    onChange((current) => ({
      ...current,
      mult_targets: (current.mult_targets || []).filter((_, currentIndex) => currentIndex !== index),
    }));
  }

  function addTarget() {
    onChange((current) => ({
      ...current,
      mult_targets: [...(current.mult_targets || []), { mult: 2, sell_pct: 50, label: "2x", triggered: false }],
    }));
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4 text-xs text-[var(--muted-foreground)]">
        <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
          <div className="mb-1 uppercase tracking-[0.2em] text-[10px]">Entry</div>
          <div className="text-white">{position.entry_sol ? `${position.entry_sol.toFixed(3)} SOL` : "n/a"}</div>
        </div>
        <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
          <div className="mb-1 uppercase tracking-[0.2em] text-[10px]">Strategy</div>
          <div className="text-white">{position.strategy_profile || config.strategy_profile || "n/a"}</div>
        </div>
        <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
          <div className="mb-1 uppercase tracking-[0.2em] text-[10px]">Exit Profile</div>
          <div className="text-white">{position.exit_profile || config.exit_profile || "n/a"}</div>
        </div>
        <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
          <div className="mb-1 uppercase tracking-[0.2em] text-[10px]">Bought</div>
          <div className="text-white">{formatTimestamp(position.purchase_timestamp || config.purchase_timestamp)}</div>
        </div>
      </div>

      <label className="flex items-center gap-3 text-sm text-white">
        <input
          type="checkbox"
          checked={Boolean(config.enabled ?? true)}
          onChange={(event) => onChange((current) => ({ ...current, enabled: event.target.checked }))}
        />
        Auto-sell enabled
      </label>

      <div className="space-y-3">
        <div className="text-sm font-medium text-white">Multiplier Targets</div>
        <div className="space-y-2">
          {targets.length > 0 ? targets.map((target, index) => (
            <div key={`${position.mint}-target-${index}`} className="grid gap-2 md:grid-cols-[1fr_1fr_auto]">
              <input
                value={String(target.mult ?? 0)}
                onChange={(event) => updateTarget(index, "mult", Number(event.target.value || 0))}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
                placeholder="Target multiple"
              />
              <input
                value={String(target.sell_pct ?? 0)}
                onChange={(event) => updateTarget(index, "sell_pct", Number(event.target.value || 0))}
                className="rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
                placeholder="Sell %"
              />
              <button
                type="button"
                onClick={() => removeTarget(index)}
                className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)]"
              >
                Remove
              </button>
            </div>
          )) : (
            <div className="text-sm text-[var(--muted-foreground)]">No multiplier targets configured yet.</div>
          )}
        </div>
        <button
          type="button"
          onClick={addTarget}
          className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)]"
        >
          Add target
        </button>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <ExitBlock
          title="Stop-Loss"
          enabled={Boolean(config.stop_loss?.enabled)}
          fields={[
            { key: "pct", label: "Drop %", value: numberOrZero(config.stop_loss?.pct) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.stop_loss?.sell_pct || 100) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              stop_loss: { ...(current.stop_loss || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              stop_loss: { ...(current.stop_loss || {}), [key]: value },
            }))
          }
        />
        <ExitBlock
          title="Trailing Stop"
          enabled={Boolean(config.trailing_stop?.enabled)}
          fields={[
            { key: "trail_pct", label: "Trail %", value: numberOrZero(config.trailing_stop?.trail_pct) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.trailing_stop?.sell_pct || 100) },
            { key: "post_partial_trail_pct", label: "After partial trail %", value: numberOrZero(config.trailing_stop?.post_partial_trail_pct) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              trailing_stop: { ...(current.trailing_stop || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              trailing_stop: { ...(current.trailing_stop || {}), [key]: value },
            }))
          }
        />
        <ExitBlock
          title="Trailing Take Profit"
          enabled={Boolean(config.trailing_tp?.enabled)}
          fields={[
            { key: "activate_mult", label: "Activate at x", value: numberOrZero(config.trailing_tp?.activate_mult) },
            { key: "trail_pct", label: "Trail %", value: numberOrZero(config.trailing_tp?.trail_pct) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.trailing_tp?.sell_pct) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              trailing_tp: { ...(current.trailing_tp || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              trailing_tp: { ...(current.trailing_tp || {}), [key]: value },
            }))
          }
        />
        <ExitBlock
          title="Time Exit"
          enabled={Boolean(config.time_exit?.enabled)}
          fields={[
            { key: "hours", label: "Hours", value: numberOrZero(config.time_exit?.hours) },
            { key: "target_mult", label: "Target x", value: numberOrZero(config.time_exit?.target_mult) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.time_exit?.sell_pct || 100) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              time_exit: { ...(current.time_exit || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              time_exit: { ...(current.time_exit || {}), [key]: value },
            }))
          }
        />
        <ExitBlock
          title="First Risk-Off"
          enabled={Boolean(config.first_risk_off?.enabled)}
          fields={[
            { key: "activate_mult", label: "Activate at x", value: numberOrZero(config.first_risk_off?.activate_mult) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.first_risk_off?.sell_pct) },
            { key: "tighten_to_pct", label: "Tighten trail to %", value: numberOrZero(config.first_risk_off?.tighten_to_pct) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              first_risk_off: { ...(current.first_risk_off || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              first_risk_off: { ...(current.first_risk_off || {}), [key]: value },
            }))
          }
        />
        <ExitBlock
          title="Velocity Roll-Over"
          enabled={Boolean(config.velocity_rollover?.enabled)}
          fields={[
            { key: "activate_mult", label: "Activate at x", value: numberOrZero(config.velocity_rollover?.activate_mult) },
            { key: "sell_pct", label: "Sell %", value: numberOrZero(config.velocity_rollover?.sell_pct) },
            { key: "min_score_drop", label: "Min score drop", value: numberOrZero(config.velocity_rollover?.min_score_drop) },
            { key: "min_velocity", label: "Min velocity", value: numberOrZero(config.velocity_rollover?.min_velocity) },
          ]}
          onToggle={(enabled) =>
            onChange((current) => ({
              ...current,
              velocity_rollover: { ...(current.velocity_rollover || {}), enabled },
            }))
          }
          onFieldChange={(key, value) =>
            onChange((current) => ({
              ...current,
              velocity_rollover: { ...(current.velocity_rollover || {}), [key]: value },
            }))
          }
        />
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)] disabled:opacity-60"
        >
          {saving ? "Saving..." : "Save exits"}
        </button>
      </div>
    </div>
  );
}

function PositionIntel({
  mint,
  snapshot,
  position,
}: {
  mint: string;
  snapshot: PositionSnapshotResponse;
  position: NonNullable<PortfolioResponse["paper"]>["positions"][number];
}) {
  const analysis = snapshot.analysis || {};
  const lifecycle = snapshot.lifecycle || {};
  const metrics = snapshot.metrics || {};
  const quality = analysis.quality_flags || {};
  const qualityReasons = [
    ...(quality.quality_reasons || []),
    ...(quality.force_scouted_reasons || []),
    ...(quality.autobuy_only_reasons || []),
  ].slice(0, 6);
  const breakdownRows = Object.entries(analysis.breakdown || {}).slice(0, 6);

  return (
    <div className="mb-5 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-white">Position Intel</div>
          <div className="mt-1 text-xs text-[var(--muted-foreground)]">
            Lifecycle-backed analysis for {position.symbol || mint.slice(0, 8)}
          </div>
        </div>
        <Link
          href={`/token/${mint}`}
          className="rounded-full border border-white/10 px-4 py-2 text-xs text-[var(--muted-foreground)] transition hover:border-white/20 hover:text-white"
        >
          Full token timeline
        </Link>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4 text-xs text-[var(--muted-foreground)]">
        <IntelStat label="State" value={String(lifecycle.state || "unknown")} />
        <IntelStat label="Source" value={String(lifecycle.source_primary || "legacy")} />
        <IntelStat label="Strategy" value={String(analysis.strategy_profile || lifecycle.strategy_profile || position.strategy_profile || "n/a")} />
        <IntelStat label="Exit Preset" value={String(analysis.strategy_exit_preset || position.exit_profile || "n/a")} />
        <IntelStat label="Effective Score" value={analysis.effective_score !== undefined ? String(analysis.effective_score) : "n/a"} />
        <IntelStat label="Confidence" value={analysis.strategy_confidence !== undefined ? Number(analysis.strategy_confidence).toFixed(2) : "n/a"} />
        <IntelStat label="Buy Ratio 5m" value={metrics.buy_ratio_5m !== undefined ? `${Math.round(Number(metrics.buy_ratio_5m) * 100)}%` : "n/a"} />
        <IntelStat label="Liquidity Delta" value={metrics.liquidity_delta_pct !== undefined ? `${Number(metrics.liquidity_delta_pct).toFixed(1)}%` : "n/a"} />
        <IntelStat label="Score Slope" value={metrics.score_slope !== undefined ? Number(metrics.score_slope).toFixed(2) : "n/a"} />
        <IntelStat label="Peak Score" value={metrics.peak_score !== undefined ? String(metrics.peak_score) : "n/a"} />
        <IntelStat label="Holder Concentration" value={metrics.holder_concentration !== undefined ? `${(Number(metrics.holder_concentration) * 100).toFixed(1)}%` : "n/a"} />
        <IntelStat label="Age" value={lifecycle.launch_ts ? formatRelativeAge(Number(lifecycle.launch_ts)) : "n/a"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
          <div className="mb-3 text-sm font-medium text-white">Operator flags</div>
          <div className="mb-3 flex flex-wrap gap-2 text-xs">
            <StatusPill label={quality.alert_blocked ? "Alert blocked" : "Alert eligible"} tone={quality.alert_blocked ? "red" : "green"} />
            <StatusPill label={quality.autobuy_blocked ? "Auto-buy blocked" : "Auto-buy eligible"} tone={quality.autobuy_blocked ? "red" : "green"} />
            {quality.force_scouted ? <StatusPill label="Force scouted" tone="amber" /> : null}
            {analysis.risk ? <StatusPill label={`Risk ${String(analysis.risk)}`} tone="slate" /> : null}
            {analysis.archetype_label ? <StatusPill label={String(analysis.archetype_label)} tone="slate" /> : null}
          </div>
          {qualityReasons.length ? (
            <div className="space-y-2">
              {qualityReasons.map((reason) => (
                <div key={reason} className="rounded-xl border border-white/6 bg-black/10 px-3 py-2 text-sm text-[var(--muted-foreground)]">
                  {reason}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-sm text-[var(--muted-foreground)]">No blocking quality reasons recorded for this position.</div>
          )}
        </div>

        <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
          <div className="mb-3 text-sm font-medium text-white">Score factors</div>
          <div className="space-y-2">
            {breakdownRows.length ? (
              breakdownRows.map(([factor, tuple]) => {
                const value = Array.isArray(tuple) ? tuple : [];
                return (
                  <div key={factor} className="rounded-xl border border-white/6 bg-black/10 px-3 py-2">
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-xs uppercase tracking-[0.16em] text-[var(--muted-foreground)]">{factor.replaceAll("_", " ")}</div>
                      <div className="text-sm font-medium text-white">{value[0] ?? 0}</div>
                    </div>
                    <div className="mt-1 text-sm text-[var(--muted-foreground)]">{String(value[1] || "n/a")}</div>
                  </div>
                );
              })
            ) : (
              <div className="text-sm text-[var(--muted-foreground)]">No score breakdown available for this position yet.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function IntelStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-3">
      <div className="mb-1 uppercase tracking-[0.2em] text-[10px]">{label}</div>
      <div className="text-white">{value}</div>
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: "green" | "red" | "amber" | "slate" }) {
  const classes =
    tone === "green"
      ? "border-emerald-400/20 bg-emerald-500/10 text-emerald-200"
      : tone === "red"
        ? "border-red-400/20 bg-red-500/10 text-red-200"
        : tone === "amber"
          ? "border-amber-400/20 bg-amber-500/10 text-amber-100"
          : "border-white/10 bg-white/5 text-white/70";
  return <div className={`rounded-full border px-3 py-1 ${classes}`}>{label}</div>;
}

function formatRelativeAge(ts: number) {
  const ageMins = Math.max(0, (Date.now() / 1000 - ts) / 60);
  if (ageMins < 1) return "<1m";
  if (ageMins < 60) return `${Math.round(ageMins)}m`;
  return `${(ageMins / 60).toFixed(1)}h`;
}

function ExitBlock({
  title,
  enabled,
  fields,
  onToggle,
  onFieldChange,
}: {
  title: string;
  enabled: boolean;
  fields: Array<{ key: string; label: string; value: number }>;
  onToggle: (enabled: boolean) => void;
  onFieldChange: (key: string, value: number) => void;
}) {
  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
      <label className="mb-3 flex items-center gap-3 text-sm text-white">
        <input type="checkbox" checked={enabled} onChange={(event) => onToggle(event.target.checked)} />
        {title}
      </label>
      <div className="grid gap-3 md:grid-cols-2">
        {fields.map((field) => (
          <div key={`${title}-${field.key}`} className="space-y-2">
            <div className="text-xs text-[var(--muted-foreground)]">{field.label}</div>
            <input
              value={String(field.value)}
              onChange={(event) => onFieldChange(field.key, Number(event.target.value || 0))}
              className="w-full rounded-2xl border border-white/10 bg-black/25 px-4 py-3 text-sm text-white outline-none"
            />
          </div>
        ))}
      </div>
    </div>
  );
}
