import type { Metadata } from "next";
import { Suspense } from "react";
import "./globals.css";
import { NavLink } from "@/components/nav-link";
import { UidBar } from "@/components/uid-bar";

export const metadata: Metadata = {
  title: "DigitalDegenX Control",
  description: "Scanner, trades, portfolio, and bot controls for DigitalDegenX.",
};

function HeaderControls() {
  return (
    <>
      <nav className="flex flex-wrap gap-3">
        <NavLink href="/" label="Overview" />
        <NavLink href="/scanner" label="Scanner" />
        <NavLink href="/watchlist" label="Watchlist" />
        <NavLink href="/top-alerts" label="Top Alerts" />
        <NavLink href="/trades" label="Trades" />
        <NavLink href="/portfolio" label="Portfolio" />
        <NavLink href="/research" label="Research" />
        <NavLink href="/intel/wallets" label="Intel" />
        <NavLink href="/settings" label="Settings" />
      </nav>
      <UidBar />
    </>
  );
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">
        <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(255,122,0,0.2),_transparent_32%),linear-gradient(180deg,_#0b1015,_#111a22_45%,_#090d12)]">
          <div className="mx-auto flex min-h-screen w-full max-w-7xl flex-col px-6 py-8">
            <header className="mb-8 flex flex-col gap-6 rounded-[32px] border border-white/10 bg-black/20 px-6 py-5 shadow-[0_24px_80px_rgba(0,0,0,0.28)] backdrop-blur">
              <div className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
                <div>
                  <p className="text-sm uppercase tracking-[0.24em] text-[var(--muted-foreground)]">DigitalDegenX</p>
                  <h1 className="text-3xl font-semibold text-white">Control Center</h1>
                </div>
                <p className="max-w-xl text-sm text-[var(--muted-foreground)]">
                  Telegram for alerts, browser for real operations. This dashboard reads directly from your bot API.
                </p>
              </div>
              <Suspense fallback={<div className="text-sm text-[var(--muted-foreground)]">Loading controls...</div>}>
                <HeaderControls />
              </Suspense>
            </header>
            <main className="flex-1">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
