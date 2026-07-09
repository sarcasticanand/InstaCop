import { notFound } from "next/navigation";

import { BAND_LABELS, BOT_USERNAME, fetchSeller } from "../../lib/api";

export const revalidate = 300; // ISR: refresh at most every 5 min

export async function generateMetadata({ params }) {
  const { handle } = await params;
  const seller = await safeFetch(handle);
  const name = seller?.display_name || `@${handle}`;
  return {
    title: `Is @${handle} legit? ${name} review & fraud check | InstaCop`,
    description: seller
      ? `@${handle}: ${BAND_LABELS[seller.risk_band].label} — matches ${seller.patterns_matched} of ${seller.patterns_total} known fraud patterns. Free automated check.`
      : `Fraud-pattern check for Instagram seller @${handle}.`,
    // E2 gate: uncorroborated verdicts must never be indexed under a real
    // business's name. The sitemap already excludes them; this closes crawlers
    // that find the URL anyway.
    robots: seller && seller.publishable ? undefined : { index: false, follow: false },
  };
}

async function safeFetch(handle) {
  try {
    return await fetchSeller(handle, { next: { revalidate: 300 } });
  } catch {
    return null;
  }
}

export default async function SellerPage({ params }) {
  const { handle } = await params;
  if (!/^[a-zA-Z0-9._]{1,30}$/.test(handle)) return notFound();

  const seller = await safeFetch(handle);

  // E2: thin algorithmic High/Caution is bot-DM-only — the public URL shows
  // the funnel, not the verdict.
  if (seller && !seller.publishable) {
    return (
      <div>
        <h1>@{handle}</h1>
        <div className="card">
          A preliminary automated review of this seller exists but hasn't been corroborated yet.
          Get the current report privately on Telegram:
          <br />
          <a className="bot-cta" href={`https://t.me/${BOT_USERNAME}?start=check_${handle}`}>
            Check @{handle} on Telegram →
          </a>
        </div>
      </div>
    );
  }

  if (!seller) {
    return (
      <div>
        <h1>@{handle}</h1>
        <div className="card">
          This seller hasn't been checked yet. Get a full report on Telegram in ~3 minutes:
          <br />
          <a className="bot-cta" href={`https://t.me/${BOT_USERNAME}?start=check_${handle}`}>
            Check @{handle} on Telegram →
          </a>
        </div>
      </div>
    );
  }

  const band = BAND_LABELS[seller.risk_band];
  const reviews = Object.entries(seller.review_counts || {});

  return (
    <div>
      <h1>
        Is @{seller.handle} legit?
        {seller.is_verified && <span className="badge-verified"> 🛡 Verified</span>}
      </h1>
      <p className="sub">Automated fraud-pattern check{seller.display_name ? ` for ${seller.display_name}` : ""}.</p>

      <div className="card">
        {seller.disputed && (
          <div className="stale-banner">⚖ Disputed — this assessment is under review following a challenge from the seller.</div>
        )}
        <div className="band" style={{ color: band.color }}>
          {band.emoji} {band.label} — matches {seller.patterns_matched} of {seller.patterns_total} known fraud patterns
        </div>
        <ul className="evidence">
          {seller.evidence.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>

        {seller.cache_state !== "fresh" && (
          <div className="stale-banner">
            ⚠️ This report is more than a week old. Trigger a fresh check via the Telegram bot below.
          </div>
        )}

        {seller.experience?.has_data ? (
          <div className="reviews">
            <h3>⭐ Buyer experience</h3>
            <div className="exp-levels">
              {Object.entries(seller.experience.categories || {}).map(([cat, level]) => (
                <span key={cat} className={`exp-badge exp-${level}`}>
                  {cat.replace("_", " ")}: {level === "none" ? "no reports" : level}
                </span>
              ))}
            </div>
            {seller.experience.summary && <p className="exp-summary">{seller.experience.summary}</p>}
          </div>
        ) : (
          <p className="meta">⭐ Customer reviews: none yet</p>
        )}

        {reviews.length > 0 && (
          <div className="reviews">
            <h3>Follow-up survey outcomes</h3>
            <ul>
              {reviews.map(([label, count]) => (
                <li key={label}>
                  {label}: {count}
                </li>
              ))}
            </ul>
          </div>
        )}

        {seller.corroboration_links?.length > 0 && (
          <div className="sources">
            <h3>Public mentions</h3>
            <ul>
              {seller.corroboration_links.map((url, i) => (
                <li key={i}>
                  <a href={url} rel="nofollow noopener">{url}</a>
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="meta">Last checked {new Date(seller.last_checked).toLocaleDateString("en-IN")}</p>

        <a className="bot-cta" href={`https://t.me/${BOT_USERNAME}?start=check_${seller.handle}`}>
          Got scammed by this seller? Report it →
        </a>
      </div>
    </div>
  );
}
