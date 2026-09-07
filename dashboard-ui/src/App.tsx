import { useCallback, useEffect, useState } from "react";
import { api, type Me } from "./api";
import { Login } from "./Login";
import { PrPage } from "./PrPage";
import { Qa } from "./Qa";
import { CommandPalette } from "./CommandPalette";
import { HowItWorks } from "./HowItWorks";
import { Integrations } from "./Integrations";
import { Learnings } from "./Learnings";
import { Queue } from "./Queue";
import { Sidebar } from "./Sidebar";
import { Skills } from "./Skills";
import { StackPage } from "./StackPage";
import { Tour } from "./Tour";
import { useLocation } from "./router";

function NotFound() {
  return (
    <>
      <h1>Not found</h1>
      <div className="card">
        <p className="muted">That page doesn't exist. Head back to your queue.</p>
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
  if (path.startsWith("/stack")) return <StackPage />;
  if (path.startsWith("/integrations") || path.startsWith("/settings"))
    return <Integrations me={me} />;
  if (path.startsWith("/learnings")) return <Learnings me={me} />;
  if (path.startsWith("/how")) return <HowItWorks />;
  return <NotFound />;
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
      <CommandPalette />
      <Tour />
    </div>
  );
}
