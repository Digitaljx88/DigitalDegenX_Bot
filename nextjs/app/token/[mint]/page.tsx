import { TokenTimelineDashboard } from "@/components/token-timeline-dashboard";

export default async function TokenTimelinePage({
  params,
}: {
  params: Promise<{ mint: string }>;
}) {
  const { mint } = await params;
  return <TokenTimelineDashboard mint={mint} />;
}
