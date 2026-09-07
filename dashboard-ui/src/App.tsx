import { useCallback, useEffect, useState } from "react";
import { api, type Me } from "./api";
import { Login } from "./Login";
import { PrPage } from "./PrPage";
import { Qa } from "./Qa";
import { Queue } from "./Queue";
import { Sidebar } from "./Sidebar";
import { Skills } from "./Skills";
import { useLocation } from "./router";

function Placeholder({ name }: { name: string }) {
  return (
    <>
      <h1>{name}</h1>
      <div className="card">
        <p className="muted">
          This page is being ported to the new React frontend. It's live in the current UI — the
          migration is porting pages one at a time.
        </p>
      </div>
    </>
  );
}

function Routed({ me }: { me: Me }) {
  const { path } = useLocation();
  if (path === "/") return <Queue me={me} />;
  if (path.startsWith("/pr")) return <PrPage me={me} />;
  if (path.startsWith("/qa")) return <Qa />;
  if (path.startsWith("/skills")) return <Skills />;
  if (path.startsWith("/integrations") || path.startsWith("/settings"))
    return <Placeholder name="Integrations" />;
  if (path.startsWith("/learnings")) return <Placeholder name="Learnings" />;
  if (path.startsWith("/how")) return <Placeholder name="How it works" />;
  if (path.startsWith("/stack")) return <Placeholder name="Stacked review" />;
  return <Placeholder name="Not found" />;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const load = useCallback(() => api.me().then(setMe), []);

  useEffect(() => {
    load();
  }, [load]);

  const signOut = useCallback(async () => {
    await api.logout();
    load();
  }, [load]);

  if (!me) return <div className="boot" />;
  if (!me.authed) return <Login me={me} onDone={load} />;

  return (
    <div className="app">
      <Sidebar me={me} onSignOut={signOut} />
      <main className="main">
        <div className="wrap">
          <Routed me={me} />
        </div>
      </main>
    </div>
  );
}
