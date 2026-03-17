// Central metadata for heat score factors and tiers.
// Used by scanner chips and settings cards — no scoring logic here.

export type FactorKey =
  | "momentum"
  | "liquidity"
  | "risk_safety"
  | "social_narrative"
  | "wallets"
  | "migration"
  | "directional_bias"
  | "volume_trend";

export type FactorMeta = {
  label: string;
  icon: string;
  color: string;
  colorRgb: string;
  maxPts: number;
  description: string;
  badgeDescription: (pts: number) => string;
};

export const FACTORS: Record<FactorKey, FactorMeta> = {
  risk_safety: {
    label: "Safety Check",
    icon: "🛡️",
    color: "#22d3a0",
    colorRgb: "34,211,160",
    maxPts: 25,
    description: "RugCheck safety score, dev dump risk, whale concentration, and bundle detection.",
    badgeDescription: (pts) => pts >= 20 ? "Clean" : pts >= 12 ? "Caution" : "Risky",
  },
  momentum: {
    label: "Volume Momentum",
    icon: "⚡",
    color: "#f97316",
    colorRgb: "249,115,22",
    maxPts: 20,
    description: "Recent USD trading volume and token age momentum combined.",
    badgeDescription: (pts) => pts >= 15 ? "Surging" : pts >= 8 ? "Building" : "Slow",
  },
  liquidity: {
    label: "Liquidity Depth",
    icon: "💧",
    color: "#60a5fa",
    colorRgb: "96,165,250",
    maxPts: 20,
    description: "Pool size in USD — deeper pools mean less slippage and lower rug risk.",
    badgeDescription: (pts) => pts >= 15 ? "Deep" : pts >= 8 ? "Moderate" : "Thin",
  },
  directional_bias: {
    label: "Buy Pressure",
    icon: "📈",
    color: "#34d399",
    colorRgb: "52,211,153",
    maxPts: 10,
    description: "Buy-to-sell ratio in recent transactions — high ratio signals accumulation.",
    badgeDescription: (pts) => pts >= 8 ? "Strong" : pts >= 4 ? "Mild" : "Weak",
  },
  social_narrative: {
    label: "Narrative Fit",
    icon: "🔥",
    color: "#a78bfa",
    colorRgb: "167,139,250",
    maxPts: 15,
    description: "How well the token matches currently trending narratives and social signals.",
    badgeDescription: (pts) => pts >= 12 ? "Trending" : pts >= 6 ? "Relevant" : "Cold",
  },
  wallets: {
    label: "Smart Money",
    icon: "🐋",
    color: "#fbbf24",
    colorRgb: "251,191,36",
    maxPts: 15,
    description: "Presence of known profitable wallets or recognized wallet clusters.",
    badgeDescription: (pts) => pts >= 12 ? "Spotted" : pts >= 6 ? "Possible" : "None",
  },
  migration: {
    label: "Token Stage",
    icon: "🚀",
    color: "#f43f5e",
    colorRgb: "244,63,94",
    maxPts: 10,
    description: "Token lifecycle stage — new launches and pump.fun graduates score higher.",
    badgeDescription: (pts) => pts >= 8 ? "Fresh" : pts >= 4 ? "Active" : "Mature",
  },
  volume_trend: {
    label: "Volume Trend",
    icon: "📊",
    color: "#818cf8",
    colorRgb: "129,140,248",
    maxPts: 5,
    description: "Direction and velocity of volume change over the last few minutes.",
    badgeDescription: (pts) => pts >= 4 ? "Explosive" : pts >= 2 ? "Rising" : "Flat",
  },
};

// Display order: safety DQ signals first, then by importance
export const FACTOR_ORDER: FactorKey[] = [
  "risk_safety",
  "momentum",
  "liquidity",
  "directional_bias",
  "social_narrative",
  "wallets",
  "migration",
  "volume_trend",
];

// ── Tier registry ──────────────────────────────────────────────────

export type TierKey = "ultra_hot" | "hot" | "warm" | "scouted" | "tracked";

export type TierMeta = {
  label: string;
  color: string;
  colorRgb: string;
  minScore: number;
};

export const TIERS: Record<TierKey, TierMeta> = {
  ultra_hot: { label: "Ultra Hot",  color: "#f97316", colorRgb: "249,115,22",  minScore: 85 },
  hot:       { label: "Hot",        color: "#fbbf24", colorRgb: "251,191,36",  minScore: 70 },
  warm:      { label: "Warm",       color: "#a78bfa", colorRgb: "167,139,250", minScore: 55 },
  scouted:   { label: "Scouted",    color: "#60a5fa", colorRgb: "96,165,250",  minScore: 35 },
  tracked:   { label: "Tracked",    color: "#8b90a8", colorRgb: "139,144,168", minScore: 0  },
};

export function scoreTierKey(score: number): TierKey {
  if (score >= TIERS.ultra_hot.minScore) return "ultra_hot";
  if (score >= TIERS.hot.minScore)       return "hot";
  if (score >= TIERS.warm.minScore)      return "warm";
  if (score >= TIERS.scouted.minScore)   return "scouted";
  return "tracked";
}

// ── Breakdown helpers ──────────────────────────────────────────────

// The scanner emits breakdown as { factor_key: [pts, reason_string] }
export type BreakdownMap = Partial<Record<string, [number, string]>>;

/**
 * Parse a raw breakdown map and return sorted entries for known factors.
 * Entries are sorted by points descending.
 */
export function parseBreakdown(
  breakdown: BreakdownMap,
): { key: FactorKey; pts: number; reason: string; meta: FactorMeta }[] {
  const results: { key: FactorKey; pts: number; reason: string; meta: FactorMeta }[] = [];
  for (const key of FACTOR_ORDER) {
    const entry = breakdown[key];
    if (!entry) continue;
    const [pts, reason] = entry;
    results.push({ key, pts, reason, meta: FACTORS[key] });
  }
  return results.sort((a, b) => b.pts - a.pts);
}

/**
 * Return the top N factors by points earned.
 */
export function topFactors(
  breakdown: BreakdownMap,
  n = 3,
): { key: FactorKey; pts: number; reason: string; meta: FactorMeta }[] {
  return parseBreakdown(breakdown).slice(0, n);
}
