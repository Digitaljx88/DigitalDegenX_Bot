import { IntelDashboard } from "@/components/intel-dashboard";

export default function PlaybookIntelPage() {
  return <IntelDashboard title="Launch Playbook" subtitle="Predictive launch archetypes and current best bets." endpoint="/intel/playbook" collectionKey="ranked_archetypes" />;
}
