import { IntelDashboard } from "@/components/intel-dashboard";

export default function WalletIntelPage() {
  return <IntelDashboard title="Wallet Intelligence" subtitle="Auto-tracked wallet performance and reputation." endpoint="/intel/wallets" collectionKey="wallets" />;
}
