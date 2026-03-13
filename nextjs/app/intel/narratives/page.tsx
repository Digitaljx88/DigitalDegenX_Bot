import { IntelDashboard } from "@/components/intel-dashboard";

export default function NarrativeIntelPage() {
  return <IntelDashboard title="Narrative Intelligence" subtitle="Trending themes and narrative performance." endpoint="/intel/narratives" collectionKey="narratives" />;
}
