import { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  badge,
  children,
}: {
  title: string;
  subtitle?: string;
  badge?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[28px] border border-white/10 bg-[var(--panel)] p-6 shadow-[0_24px_80px_rgba(0,0,0,0.22)]">
      <div className="mb-5 flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-white">{title}</h2>
          {badge && (
            <span className="rounded-full bg-[var(--accent)]/15 px-2 py-0.5 text-[10px]
                             font-semibold uppercase tracking-widest text-[var(--accent)]">
              {badge}
            </span>
          )}
        </div>
        {subtitle ? <p className="text-sm text-[var(--muted-foreground)]">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}
