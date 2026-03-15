export function ExternalLinks({ mint, className }: { mint: string; className?: string }) {
  return (
    <div className={`flex items-center gap-1.5 ${className ?? ""}`}>
      <a
        href={`https://pump.fun/coin/${mint}`}
        target="_blank"
        rel="noopener noreferrer"
        className="rounded px-1.5 py-0.5 text-[10px] font-medium text-white/30 hover:bg-white/8 hover:text-white/70"
        title="View on pump.fun"
      >
        pump
      </a>
      <a
        href={`https://dexscreener.com/solana/${mint}`}
        target="_blank"
        rel="noopener noreferrer"
        className="rounded px-1.5 py-0.5 text-[10px] font-medium text-white/30 hover:bg-white/8 hover:text-white/70"
        title="View on DexScreener"
      >
        dex
      </a>
    </div>
  );
}
