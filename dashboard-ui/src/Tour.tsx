import { useCallback, useEffect, useRef, useState } from "react";
import { navigate } from "./router";

interface TourStep {
  sel?: string;
  title: string;
  text: string;
  cta?: { label: string; to: string };
}

const TOUR: TourStep[] = [
  {
    title: "Welcome to Robin 👋",
    text: "Robin drafts the PR reviews you owe your team. You tick what's worth saying and post it — as yourself. Here's the 30-second setup.",
  },
  {
    sel: '[data-tour="integrations"]',
    title: "1. Connect your accounts",
    text: "Start in Integrations: add your Claude account (reviews run on it) and your Slack member ID (so Robin can ping you when a review is requested).",
  },
  {
    sel: '[data-tour="skills"]',
    title: "2. Pick a review skill",
    text: "Robin follows a review skill. The shared team default works out of the box — or bring your own here. You can switch any time.",
  },
  {
    sel: '[data-tour="queuelist"]',
    title: "3. Your review queue",
    text: "PRs waiting on your review land here. Open one, choose an effort level, and Robin drafts the review — nothing posts to GitHub without your click.",
  },
  {
    title: "You're set 🎉",
    text: "Open a PR from your queue to run your first review. You stay the reviewer — Robin just does the reading and drafting.",
    cta: { label: "Go to Integrations", to: "/integrations" },
  },
];

const SEEN_KEY = "robin_tour";
const EVT = "robin:start-tour";

// Fire from anywhere (e.g. the sidebar) to (re)open the tour.
export function startTour() {
  window.dispatchEvent(new Event(EVT));
}

export function Tour() {
  const [open, setOpen] = useState(false);
  const [i, setI] = useState(0);
  const ringRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  const end = useCallback(() => {
    try {
      localStorage.setItem(SEEN_KEY, "done");
    } catch {
      /* private mode */
    }
    setOpen(false);
  }, []);

  // Open on demand, and auto-start once on the queue for first-timers.
  useEffect(() => {
    const onStart = () => {
      setI(0);
      setOpen(true);
    };
    window.addEventListener(EVT, onStart);
    let seen = false;
    try {
      seen = localStorage.getItem(SEEN_KEY) === "done";
    } catch {
      /* private mode */
    }
    let t: number | undefined;
    if (!seen && document.querySelector('[data-tour="queuelist"]')) {
      t = window.setTimeout(onStart, 450);
    }
    return () => {
      window.removeEventListener(EVT, onStart);
      window.clearTimeout(t);
    };
  }, []);

  // Position the ring + card against the current step's target.
  const place = useCallback(() => {
    const s = TOUR[i];
    const ring = ringRef.current;
    const card = cardRef.current;
    if (!ring || !card) return;
    const tgt = s.sel ? (document.querySelector(s.sel) as HTMLElement | null) : null;
    if (tgt) {
      tgt.scrollIntoView({ block: "center", behavior: "smooth" });
      const r = tgt.getBoundingClientRect();
      const pad = 6;
      ring.style.display = "block";
      ring.style.left = `${r.left - pad}px`;
      ring.style.top = `${r.top - pad}px`;
      ring.style.width = `${r.width + pad * 2}px`;
      ring.style.height = `${r.height + pad * 2}px`;
      card.style.left = `${Math.min(window.innerWidth - 360, Math.max(16, r.right + 14))}px`;
      card.style.top = `${Math.max(16, r.top)}px`;
      card.style.transform = "none";
    } else {
      ring.style.display = "none";
      card.style.left = "50%";
      card.style.top = "50%";
      card.style.transform = "translate(-50%,-50%)";
    }
  }, [i]);

  useEffect(() => {
    if (!open) return;
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, place]);

  if (!open) return null;
  const s = TOUR[i];
  const dimmed = !s.sel;
  const last = i === TOUR.length - 1;

  return (
    <div className={"tourv" + (dimmed ? " dim" : "")}>
      <div className="tourmask" onClick={end} />
      <div className="tourring" ref={ringRef} />
      <div className="tourcard" ref={cardRef}>
        <div className="tourh">{s.title}</div>
        <div className="tourtext">{s.text}</div>
        <div className="tourdots">
          {TOUR.map((_, j) => (
            <span key={j} className={"tdot" + (j === i ? " on" : "")} />
          ))}
        </div>
        <div className="tourbtns">
          <button className="btn ghost" onClick={end}>
            Skip
          </button>
          <span className="spacer" />
          {i > 0 && (
            <button className="btn soft" onClick={() => setI((n) => n - 1)}>
              Back
            </button>
          )}
          {s.cta ? (
            <button
              className="btn primary"
              onClick={() => {
                navigate(s.cta!.to);
                end();
              }}
            >
              {s.cta.label}
            </button>
          ) : (
            <button
              className="btn primary"
              onClick={() => (last ? end() : setI((n) => n + 1))}
            >
              {last ? "Done" : "Next"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
