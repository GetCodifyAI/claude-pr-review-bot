import { useState } from "react";
import { api, type Me } from "./api";

const NEW_TOKEN =
  "https://github.com/settings/tokens/new?" +
  new URLSearchParams({ scopes: "repo", description: "Robin — Cut+Dry PR reviews" }).toString();

export function Login({ me, onDone }: { me: Me; onDone: () => void }) {
  const [pat, setPat] = useState("");
  const [busy, setBusy] = useState(false);
  // Seed from ?err= so an OAuth failure (redirected here by the server) is shown.
  const [err, setErr] = useState(() => {
    try {
      return new URLSearchParams(window.location.search).get("err") || "";
    } catch {
      return "";
    }
  });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!pat.trim()) return;
    setBusy(true);
    setErr("");
    try {
      await api.login(pat.trim());
      onDone();
    } catch (x) {
      setErr(x instanceof Error ? x.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="authcard">
        {me.logo && <img className="authlogo" src={me.logo} alt="" />}
        <h1>{me.brand}</h1>
        <p className="authsub">Your PR reviewer for Cut&amp;Dry</p>
        <p className="authlead">
          Sign in with a GitHub token. Every comment and approval posts under your own name —
          nothing is ever posted for you.
        </p>
        {me.oauth && (
          <a className="btn soft block" href="/prbot/oauth/start">
            Sign in with GitHub
          </a>
        )}
        <form onSubmit={submit}>
          {err && (
            <div className="banner err">
              <span>🚫</span>
              <div>{err}</div>
            </div>
          )}
          <div className="tokfield">
            <input
              className="in"
              type="password"
              placeholder="ghp_…"
              autoComplete="off"
              spellCheck={false}
              value={pat}
              onChange={(e) => setPat(e.target.value)}
            />
          </div>
          <button className="btn primary block" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="authfine">
          Need a token? <a href={NEW_TOKEN} target="_blank" rel="noopener">Create one</a> with the{" "}
          <code>repo</code> scope, then paste it above.
        </p>
      </div>
    </div>
  );
}
