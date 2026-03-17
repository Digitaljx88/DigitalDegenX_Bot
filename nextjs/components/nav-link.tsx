"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function NavLink({ href, label }: { href: string; label: string }) {
  const pathname = usePathname();
  const active = href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(href + "/");
  return (
    <Link
      href={href}
      className="px-4 py-2 whitespace-nowrap transition-colors rounded-t-md"
      style={{
        fontSize: 13,
        fontWeight: 500,
        color: active ? "var(--accent)" : "var(--text3)",
        borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
        marginBottom: -1,
      }}
      onMouseEnter={(e) => {
        if (!active) (e.currentTarget as HTMLAnchorElement).style.color = "var(--text2)";
      }}
      onMouseLeave={(e) => {
        if (!active) (e.currentTarget as HTMLAnchorElement).style.color = "var(--text3)";
      }}
    >
      {label}
    </Link>
  );
}
