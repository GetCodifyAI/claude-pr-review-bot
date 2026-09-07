import { useEffect, useState } from "react";
import { api, type HowData } from "./api";

interface Step {
  n: number;
  title: string;
  txt: React.ReactNode;
  img?: string;
}

const STEPS: Step[] = [
  {
    n: 1,
    title: "A review is requested",
    txt: "A teammate adds you as a reviewer on a PR. Robin notices within 3 minutes and sends you a Slack card — no need to watch GitHub.",
    img: "slack",
  },
  {
    n: 2,
    title: "Robin drafts the review",
    txt: (
      <>
        You click through to the PR. Robin checks out the branch and runs the{" "}
        <code>pr-review</code> skill against the real diff — about 10–15 minutes for a 25-file PR.
        Nothing is posted to GitHub in this step.
      </>
    ),
    img: "progress",
  },
  {
    n: 3,
    title: "You decide what's worth saying",
    txt: (
      <>
        The findings appear as editable cards, each with a severity and a <code>file:line</code>.
        Tick the ones you agree with, edit any wording, ignore the rest.
      </>
    ),
    img: "findings",
  },
  {
    n: 4,
    title: "Post — as you",
    txt: "The selected comments post to the PR as inline review comments under your own GitHub name. Always a plain COMMENT review — Robin never requests changes or blocks a merge.",
    img: "post",
  },
  {
    n: 5,
    title: "Approve when you're ready",
    txt: "A separate click posts an LGTM comment and approves the PR, also as you. This is what stops the 'posted comments, forgot to approve' loop.",
    img: "approve",
  },
];

const GUARANTEES = [
  { t: "🧑 Always you", d: "Every comment and approval posts under your own GitHub account." },
  {
    t: "🖱️ Nothing automatic",
    d: "Nothing reaches GitHub without your click. The review step can't write to GitHub at all.",
  },
  {
    t: "💬 Comments, not blocks",
    d: "Robin posts plain review comments — it never requests changes or blocks a merge.",
  },
  {
    t: "🔒 Your credentials, encrypted",
    d: "Your GitHub and Claude tokens are encrypted on the box and used only for your actions.",
  },
];

export function HowItWorks() {
  const [d, setD] = useState<HowData | null>(null);
  useEffect(() => {
    api.how().then(setD);
  }, []);
  if (!d) return <div className="muted">Loading…</div>;

  return (
    <>
      <h1>How {d.brand} works</h1>
      <p className="lead">
        Robin drafts the PR reviews you owe your team, and lets you send them with a click. It's an
        assistant — you stay the reviewer.
      </p>
      <div className="flow">
        {STEPS.map((s) => (
          <div className="fstep" key={s.n}>
            <div className="fn">{s.n}</div>
            <div className="fb">
              <h4>{s.title}</h4>
              <div className="muted sm">{s.txt}</div>
              {s.img && d.images[s.img] && (
                <div className="shot">
                  <img src={d.images[s.img]} alt="" loading="lazy" />
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      <h2>The guarantees</h2>
      <div className="grid2">
        {GUARANTEES.map((g) => (
          <div className="mini" key={g.t}>
            <div className="t">{g.t}</div>
            <div className="muted sm">{g.d}</div>
          </div>
        ))}
      </div>
      <h2>What the tabs mean</h2>
      <div className="grid2">
        {d.tabs.map((t) => (
          <div className="mini" key={t.key}>
            <div className="t">{t.label}</div>
            <div className="muted sm">{t.desc}</div>
          </div>
        ))}
      </div>
      <p className="fine">
        Questions or something not working? Ping <code>{d.reviewer}</code>.
      </p>
    </>
  );
}
