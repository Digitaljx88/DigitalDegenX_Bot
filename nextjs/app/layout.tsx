import type { Metadata } from "next";
import { Suspense } from "react";
import "./globals.css";
import { NavLink } from "@/components/nav-link";
import { UidBar } from "@/components/uid-bar";
import { TickerBar } from "@/components/ticker-bar";
import { ApiStatusDot } from "@/components/api-status-dot";

export const metadata: Metadata = {
  title: "DigitalDegenX Control",
  description: "Scanner, trades, portfolio, and bot controls for DigitalDegenX.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="antialiased">
        {/* ── Ticker ── */}
        <Suspense fallback={null}>
          <TickerBar />
        </Suspense>

        {/* ── Header ── */}
        <div className="px-8 pt-7 flex items-start justify-between gap-6 flex-wrap">
          <div className="flex flex-col gap-1">
            <span
              style={{
                fontSize: 10,
                letterSpacing: "0.18em",
                textTransform: "uppercase",
                fontWeight: 500,
                color: "var(--text3)",
              }}
            >
              DigitalDegenX
            </span>
            <h1
              className="flex items-center gap-2.5 text-white"
              style={{ fontSize: 26, fontWeight: 700, letterSpacing: "-0.02em" }}
            >
              Control Center
              <Suspense fallback={<span className="h-2 w-2 rounded-full bg-white/20" />}>
                <ApiStatusDot />
              </Suspense>
            </h1>
            <p
              className="mt-1 leading-relaxed"
              style={{ fontSize: 12, color: "var(--text3)", maxWidth: 420 }}
            >
              Real-time scanner, portfolio exits, and auto-buy config. Telegram handles alerts; this dashboard handles everything else.
            </p>
          </div>
          <Suspense fallback={null}>
            <UidBar />
          </Suspense>
        </div>

        {/* ── Nav ── */}
        <nav
          className="flex gap-0.5 px-8 mt-5"
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <NavLink href="/" label="Overview" />
          <NavLink href="/scanner" label="Scanner" />
          <NavLink href="/watchlist" label="Watchlist" />
          <NavLink href="/top-alerts" label="Top Alerts" />
          <NavLink href="/trades" label="Trades" />
          <NavLink href="/autobuy" label="Auto-Buy" />
          <NavLink href="/sniper" label="Sniper" />
          <NavLink href="/portfolio" label="Portfolio" />
          <NavLink href="/research" label="Research" />
          <NavLink href="/intel/wallets" label="Intel" />
          <NavLink href="/settings" label="Settings" />
        </nav>

        {/* ── Content ── */}
        <main className="px-8 py-6">{children}</main>
      </body>
    </html>
  );
}
