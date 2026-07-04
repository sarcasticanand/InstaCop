export const API_URL = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
export const BOT_USERNAME = process.env.NEXT_PUBLIC_BOT_USERNAME || "TrustKaroBot";

export const BAND_LABELS = {
  low: { emoji: "🟢", label: "Low risk", color: "#15803d" },
  caution: { emoji: "🟡", label: "Caution", color: "#a16207" },
  high: { emoji: "🔴", label: "High risk", color: "#b91c1c" },
  insufficient: { emoji: "⚪", label: "Insufficient information", color: "#525252" },
};

export async function fetchSeller(handle, opts = {}) {
  const res = await fetch(`${API_URL}/api/sellers/${encodeURIComponent(handle)}`, opts);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}
