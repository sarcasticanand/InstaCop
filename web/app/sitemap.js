import { API_URL } from "../lib/api";

const BASE = process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000";

export default async function sitemap() {
  let sellers = [];
  try {
    const res = await fetch(`${API_URL}/api/sellers?limit=5000`, { next: { revalidate: 3600 } });
    sellers = (await res.json()).sellers || [];
  } catch {
    // API down: still emit the home page
  }
  return [
    { url: BASE, changeFrequency: "daily", priority: 1 },
    ...sellers.map((s) => ({
      url: `${BASE}/${s.handle}`,
      lastModified: s.last_checked,
      changeFrequency: "weekly",
      priority: 0.7,
    })),
  ];
}
