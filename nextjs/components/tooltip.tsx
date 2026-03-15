"use client";

export function Tooltip({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <span className="group relative inline-flex items-center">
      {children ?? (
        <span className="ml-1 inline-flex h-3.5 w-3.5 cursor-help items-center justify-center
                         rounded-full border border-white/20 text-[9px] text-white/40
                         hover:border-white/40 hover:text-white/70">
          ?
        </span>
      )}
      <span className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 w-56
                       -translate-x-1/2 rounded-xl border border-white/10 bg-[#0d1a25]
                       px-3 py-2 text-xs leading-relaxed text-[var(--muted-foreground)]
                       opacity-0 shadow-xl transition-opacity group-hover:opacity-100">
        {text}
        <span className="absolute left-1/2 top-full -mt-px h-2 w-2 -translate-x-1/2
                         rotate-45 border-b border-r border-white/10 bg-[#0d1a25]" />
      </span>
    </span>
  );
}
