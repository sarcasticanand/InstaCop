import "./globals.css";

export const metadata = {
  title: "TrustKaro — check an Instagram seller before you pay",
  description:
    "Free fraud-pattern check for Instagram shops in India. Paste a link, see the risk report.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <a href="/" className="logo">TrustKaro</a>
          <span className="tagline">check before you pay</span>
        </header>
        <main>{children}</main>
        <footer className="site-footer">
          <p>
            TrustKaro scores are automated pattern analysis of publicly available information,
            combined with user-submitted opinions. They are not accusations, verdicts, or
            statements of fact about any person or business. Sellers can request corrections
            or takedowns at <a href="mailto:grievance@trustkaro.example">grievance@trustkaro.example</a>;
            we respond within 7 days.
          </p>
        </footer>
      </body>
    </html>
  );
}
