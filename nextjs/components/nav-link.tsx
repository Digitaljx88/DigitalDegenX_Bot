"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSearchParams } from "next/navigation";

export function NavLink({ href, label }: { href: string; label: string }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const active = pathname === href;
  const uid = searchParams.get("uid");
  const target = uid ? `${href}?uid=${uid}` : href;
  return (
    <Link
      href={target}
      className={`rounded-full px-4 py-2 text-sm font-medium transition ${
        active ? "bg-[var(--accent)] text-[var(--accent-foreground)]" : "text-[var(--muted-foreground)] hover:bg-white/8 hover:text-white"
      }`}
    >
      {label}
    </Link>
  );
}
