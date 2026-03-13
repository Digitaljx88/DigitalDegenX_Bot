import { IntelDashboard } from "@/components/intel-dashboard";

export default function ClusterIntelPage() {
  return <IntelDashboard title="Cluster Intelligence" subtitle="Top co-investing wallet pairs and recent cluster events." endpoint="/intel/clusters" collectionKey="top_pairs" />;
}
