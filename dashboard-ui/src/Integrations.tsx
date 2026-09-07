import { useCallback, useEffect, useState } from "react";
import { api, type IntegrationsData, type Me } from "./api";
import { BrandIcon } from "./icons";

function Banner({ html }: { html: string }) {
  if (!html) return null;
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}

function Card({
  icon,
  cls,
  name,
  chip,
  sub,
  ok,
  children,
}: {
  icon: React.ReactNode;
  cls: string;
  name: string;
  chip: React.ReactNode;
  sub: React.ReactNode;
  ok?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={"intg" + (ok ? " ok" : "")}>
      <div className="itop">
        <div className={"iico " + cls}>{icon}</div>
        <div className="imeta">
          <div className="iname">
            {name} {chip}
          </div>
          <div className="idesc">{sub}</div>
        </div>
      </div>
      <div className="ictl">{children}</div>
    </div>
  );
}

const ON = <span className="tag-on">Connected</span>;
const OFF = <span className="tag-off">Not connected</span>;
const REQ = <span className="tag-req">Required</span>;

function GithubCtl({ token, onDone }: { token: IntegrationsData["token"]; onDone: (b: string) => void }) {
  const [show, setShow] = useState(false);
  const [pat, setPat] = useState("");
  if (!show)
    return (
      <button type="button" className="replace" onClick={() => setShow(true)}>
        Replace token
      </button>
    );
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        const r = await api.saveSettings(token, { pat });
        setPat("");
        setShow(false);
        onDone(r.bannerHtml);
      }}
    >
      <div className="inrow">
        <input
          type="password"
          className="in"
          placeholder="ghp_…"
          autoComplete="off"
          spellCheck={false}
          value={pat}
          onChange={(e) => setPat(e.target.value)}
        />
        <button className="btn primary" type="submit">
          Save
        </button>
      </div>
    </form>
  );
}

function SlackCtl({
  token,
  value,
  onDone,
}: {
  token: IntegrationsData["token"];
  value: string;
  onDone: (b: string) => void;
}) {
  const [slack, setSlack] = useState(value);
  useEffect(() => setSlack(value), [value]);
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        const r = await api.saveSettings(token, { slack_id: slack.trim() });
        onDone(r.bannerHtml);
      }}
    >
      <div className="inrow">
        <input
          type="text"
          className="in"
          placeholder="U0123ABCDEF"
          autoComplete="off"
          spellCheck={false}
          value={slack}
          onChange={(e) => setSlack(e.target.value)}
        />
        <button className="btn primary" type="submit">
          Save
        </button>
      </div>
      <div className="hint">
        In Slack: your <b>profile picture</b> → <b>Profile</b> → the <b>⋮</b> menu →{" "}
        <b>Copy member ID</b>.
      </div>
    </form>
  );
}

function ClaudeCtl({
  d,
  onDone,
}: {
  d: IntegrationsData;
  onDone: (b: string) => void;
}) {
  const [reveal, setReveal] = useState(false);
  const [code, setCode] = useState("");
  if (d.claude.connected)
    return (
      <>
        <div className="hint ok">
          ✓ Connected — reviews you start run on your own Claude account.
        </div>
        <button
          className="discbtn"
          type="button"
          onClick={async () => {
            const r = await api.claudeDisconnect(d.token);
            onDone(r.bannerHtml);
          }}
        >
          Disconnect
        </button>
      </>
    );
  return (
    <>
      <a
        className="btn primary block"
        target="_blank"
        rel="noopener"
        href={d.claude.authUrl}
        onClick={() => setReveal(true)}
      >
        {BrandIcon.claude}
        <span className="lbl">{reveal ? "Reopen Claude" : "Connect with Claude"}</span>
      </a>
      <div className="hint">
        Opens Claude in a new tab — sign in with <em>your</em> account and click <b>Authorize</b>.
        Claude shows you a code; paste it below.
      </div>
      {reveal && (
        <div style={{ marginTop: 12 }}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const r = await api.claudeCode(d.token, code.trim());
              setCode("");
              onDone(r.bannerHtml);
            }}
          >
            <div className="inrow">
              <input
                type="text"
                className="in"
                autoFocus
                placeholder="paste the code from Claude"
                autoComplete="off"
                spellCheck={false}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
              <button className="btn primary" type="submit">
                Connect
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}

export function Integrations({ me }: { me: Me }) {
  const [d, setD] = useState<IntegrationsData | null>(null);
  const [banner, setBanner] = useState("");
  const load = useCallback(() => api.integrations().then(setD), []);
  useEffect(() => {
    load();
  }, [load]);
  const onDone = (b: string) => {
    setBanner(b);
    load();
  };

  if (!d) return <div className="muted">Loading…</div>;
  const hasSlack = !!d.slack.id;
  const hasClaude = d.claude.connected;

  return (
    <>
      <h1>Integrations</h1>
      <p className="lead">
        The services {me.brand} connects to. Everything is stored encrypted on this box and used
        only on your behalf.
      </p>
      <Banner html={banner} />
      <Card
        icon={BrandIcon.gh}
        cls="gh"
        name="GitHub"
        chip={ON}
        ok
        sub={
          <>
            Connected as <code>{d.github.login}</code> — comments and approvals post under your
            name.
          </>
        }
      >
        <GithubCtl token={d.token} onDone={onDone} />
      </Card>
      <Card
        icon={BrandIcon.slack}
        cls="slack"
        name="Slack"
        chip={hasSlack ? ON : OFF}
        ok={hasSlack}
        sub="Pings you when a review is requested."
      >
        <SlackCtl token={d.token} value={d.slack.id} onDone={onDone} />
      </Card>
      <Card
        icon={BrandIcon.claude}
        cls="claude"
        name="Claude"
        chip={hasClaude ? ON : REQ}
        ok={hasClaude}
        sub={
          <>
            <b>Required to review.</b> Reviews and QA guides run on your own Claude subscription —
            never a shared account.
          </>
        }
      >
        <ClaudeCtl d={d} onDone={onDone} />
      </Card>
    </>
  );
}
