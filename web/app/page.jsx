"use client";

import { useState } from "react";
import { BAND_LABELS, BOT_USERNAME } from "../lib/api";

function parseHandle(text) {
  const url = text.match(/instagram\.com\/([a-zA-Z0-9._]{1,30})/);
  if (url) return url[1].toLowerCase();
  const bare = text.trim().replace(/^@/, "");
  if (/^[a-zA-Z0-9._]{1,30}$/.test(bare)) return bare.toLowerCase();
  return null;
}

export default function Home() {
  const [input, setInput] = useState("");
  const [state, setState] = useState({ status: "idle" });

  async function onSubmit(e) {
    e.preventDefault();
    const handle = parseHandle(input);
    if (!handle) {
      setState({ status: "error", message: "That doesn't look like an Instagram link or handle." });
      return;
    }
    setState({ status: "loading" });
    try {
      const res = await fetch(`/api/proxy/sellers/${handle}`);
      if (res.status === 404) {
        setState({ status: "unknown", handle });
        return;
      }
      const data = await res.json();
      setState({ status: "found", data });
    } catch {
      setState({ status: "error", message: "Something went wrong — try again." });
    }
  }

  const band = state.data ? BAND_LABELS[state.data.risk_band] : null;

  return (
    <div>
      <h1>Is that Instagram shop legit?</h1>
      <p className="sub">Paste the shop's link or @handle. Free, takes seconds.</p>

      <form className="checker-form" onSubmit={onSubmit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="instagram.com/shop_name or @shop_name"
          aria-label="Instagram handle or link"
        />
        <button type="submit">Check</button>
      </form>

      {state.status === "loading" && <div className="card">Checking…</div>}

      {state.status === "error" && <div className="card">{state.message}</div>}

      {state.status === "unknown" && (
        <div className="card">
          <strong>@{state.handle}</strong> hasn't been checked yet.
          <br />
          Get the full report on Telegram in ~3 minutes:
          <br />
          <a className="bot-cta" href={`https://t.me/${BOT_USERNAME}?start=check_${state.handle}`}>
            Check on Telegram →
          </a>
        </div>
      )}

      {state.status === "found" && state.data && (
        <div className="card">
          <div>
            <strong>@{state.data.handle}</strong>
            {state.data.is_verified && <span className="badge-verified"> 🛡 Verified seller</span>}
          </div>
          <div className="band" style={{ color: band.color }}>
            {band.emoji} {band.label} — matches {state.data.patterns_matched} of {state.data.patterns_total} fraud patterns
          </div>
          <ul className="evidence">
            {state.data.evidence.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
          <p className="meta">
            Last checked {new Date(state.data.last_checked).toLocaleDateString("en-IN")} ·{" "}
            <a href={`/${state.data.handle}`}>Full report →</a>
          </p>
        </div>
      )}
    </div>
  );
}
