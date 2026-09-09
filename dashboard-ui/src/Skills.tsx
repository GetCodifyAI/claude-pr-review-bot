import { useCallback, useEffect, useState } from "react";
import { api, type SkillsData, type Token } from "./api";

function Banner({ html }: { html: string }) {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}

const COPY_HINT = (
  <div className="hint">
    Load a local skill onto the clipboard, then paste it here:
    <br />
    <code>cat ~/.claude/skills/pr-review/SKILL.md | pbcopy</code> (macOS) ·{" "}
    <code>… | xclip -selection clipboard</code> or <code>… | wl-copy</code> (Linux).
  </div>
);

function RuleForm({
  token,
  target,
  onDone,
}: {
  token: Token;
  target: "global" | "me";
  onDone: (b: string) => void;
}) {
  const [rule, setRule] = useState("");
  return (
    <div className="rulebox">
      <div className="rule-lbl">Quick-add a rule</div>
      <form
        className="rulerow"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!rule.trim()) return;
          const r = await api.skillAction("rule", { ...token, target, from: "skills", rule });
          setRule("");
          onDone(r.bannerHtml);
        }}
      >
        <input
          className="in"
          autoComplete="off"
          placeholder="e.g. Don’t ask for a Jira ticket link in code comments"
          value={rule}
          onChange={(e) => setRule(e.target.value)}
        />
        <button className="btn soft" type="submit">
          Add rule
        </button>
      </form>
      <div className="hint">
        Type a preference in plain words — Robin tidies it into the skill so you don't have to edit
        the whole file.
      </div>
    </div>
  );
}

function SkillEditor({
  token,
  target,
  value,
  onDone,
}: {
  token: Token;
  target: "global" | "me";
  value: string;
  onDone: (b: string) => void;
}) {
  const [text, setText] = useState(value);
  const [confirm, setConfirm] = useState("");
  useEffect(() => setText(value), [value]);
  const isGlobal = target === "global";
  return (
    <>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const r = await api.skillAction("save", { ...token, target, from: "skills", skill: text });
          onDone(r.bannerHtml);
        }}
      >
        <textarea
          className="in"
          spellCheck={false}
          style={{ minHeight: 150, fontSize: "12.5px" }}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={
            isGlobal
              ? "The shared reviewing approach — edit it right here."
              : "Paste your pr-review SKILL.md here — or leave blank to use the team default."
          }
        />
        <div className="hint">
          {isGlobal
            ? "Everyone without their own skill uses this. Robin always appends its output format."
            : "Your skill's logic runs; Robin always appends its output format. Reviews others start are unaffected."}
        </div>
        {COPY_HINT}
        <div className="inrow" style={{ marginTop: 10 }}>
          <button className="btn primary" type="submit">
            Save skill
          </button>
          {!isGlobal && value && (
            <button
              className="btn soft"
              type="button"
              onClick={async () => {
                const r = await api.skillAction("reset", { ...token, target, from: "skills" });
                onDone(r.bannerHtml);
              }}
            >
              Clear (use team default)
            </button>
          )}
        </div>
      </form>
      {isGlobal && (
        <details className="restorebox">
          <summary>Restore built-in skill…</summary>
          <form
            className="rulerow"
            style={{ marginTop: 8 }}
            onSubmit={async (e) => {
              e.preventDefault();
              const r = await api.skillAction("restore", {
                ...token,
                target: "global",
                from: "skills",
                confirm,
              });
              setConfirm("");
              onDone(r.bannerHtml);
            }}
          >
            <input
              className="in"
              autoComplete="off"
              placeholder="Type RESTORE to confirm"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
            <button className="btn warn" type="submit">
              Restore
            </button>
          </form>
          <div className="hint">
            Discards the team's edits and reverts everyone to the built-in review skill.
          </div>
        </details>
      )}
      <RuleForm token={token} target={target} onDone={onDone} />
    </>
  );
}

function DepthEditor({ token, level, d, onDone }: {
  token: Token;
  level: string;
  d: SkillsData["depths"][string];
  onDone: (b: string) => void;
}) {
  const [text, setText] = useState(d.content);
  useEffect(() => setText(d.content), [d.content]);
  return (
    <details className="skilled">
      <summary>
        {d.name} depth{" "}
        <span className={d.edited ? "tag-on" : "tag-off"}>{d.edited ? "Edited" : "Default"}</span>
      </summary>
      <div className="dbody">
        <p className="muted sm" style={{ marginTop: 0 }}>
          What Robin does on a <b>{d.name}</b> review ({d.meta}). Appended to whichever skill runs.
        </p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const r = await api.skillAction("save", {
              ...token,
              target: `effort_${level}`,
              from: "skills",
              skill: text,
            });
            onDone(r.bannerHtml);
          }}
        >
          <textarea
            className="in"
            spellCheck={false}
            style={{ minHeight: 150, fontSize: "12.5px" }}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="inrow" style={{ marginTop: 10 }}>
            <button className="btn primary" type="submit">
              Save depth
            </button>
            {d.edited && (
              <button
                className="btn soft"
                type="button"
                onClick={async () => {
                  const r = await api.skillAction("reset", {
                    ...token,
                    target: `effort_${level}`,
                    from: "skills",
                  });
                  onDone(r.bannerHtml);
                }}
              >
                Reset to default
              </button>
            )}
          </div>
        </form>
      </div>
    </details>
  );
}

export function Skills() {
  const [d, setD] = useState<SkillsData | null>(null);
  const [banner, setBanner] = useState("");
  const load = useCallback(() => api.skills().then(setD), []);
  useEffect(() => {
    load();
  }, [load]);
  const onDone = (b: string) => {
    setBanner(b);
    load();
  };

  if (!d) return <div className="muted">Loading…</div>;

  const opt = (v: "team" | "own", name: string, sub: string, dis: boolean) => (
    <label className={"eff" + (d.choice === v ? " hot" : "") + (dis ? " off" : "")}>
      <input
        type="radio"
        name="choice"
        checked={d.choice === v}
        disabled={dis}
        onChange={async () => {
          const r = await api.skillAction("use", { ...d.token, choice: v, from: "skills" });
          onDone(r.bannerHtml);
        }}
      />
      <span className="effname">{name}</span>
      <span className="effsub">{sub}</span>
    </label>
  );

  return (
    <>
      <h1>Review skills</h1>
      <p className="lead">
        The skill is the reviewing approach Robin follows. Pick which one runs your reviews; edit it
        below. Quick / Standard / Deep all use the same skill — they differ only in the review-depth
        instructions, which you can edit too.
      </p>
      {banner && <Banner html={banner} />}

      <div className="card">
        <h4 style={{ marginTop: 0 }}>Which skill runs your reviews?</h4>
        <div className="skillsel">
          {opt("team", "Team default", "the shared reviewing approach", false)}
          {opt(
            "own",
            "My own skill",
            d.hasMySkill ? "your personal skill" : "add a skill below to use it",
            !d.hasMySkill
          )}
        </div>
        <div className="hint">
          Reviews run with <b>{d.effLabel}</b>. Learnings sharpen whichever skill runs — every
          finding you keep or drop feeds the next review.
        </div>
      </div>

      <h2>The skill</h2>
      <details className="skilled">
        <summary>
          Edit the team default skill{" "}
          <span className={d.hasGlobal ? "tag-on" : "tag-off"}>{d.hasGlobal ? "Edited" : "Built-in"}</span>
        </summary>
        <div className="dbody">
          <p className="muted sm" style={{ marginTop: 0 }}>
            The shared skill everyone falls back to. Editing it changes reviews for everyone without
            their own.
          </p>
          <SkillEditor token={d.token} target="global" value={d.teamSkill} onDone={onDone} />
          {d.teamHistory && d.teamHistory.length > 0 && (
            <div className="skillhist">
              <div className="skillhist-h">Revision history — how the team standard evolved</div>
              <ul>
                {d.teamHistory.map((h) => (
                  <li key={h.hash}>
                    <span className="skillhist-msg">{h.msg}</span>
                    <span className="skillhist-meta">
                      {h.author} · {new Date(h.at * 1000).toLocaleDateString("en-US")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </details>
      <details className="skilled">
        <summary>
          Edit your own skill{" "}
          <span className={d.hasMySkill ? "tag-on" : "tag-off"}>{d.hasMySkill ? "Custom" : "None yet"}</span>
        </summary>
        <div className="dbody">
          <SkillEditor token={d.token} target="me" value={d.mySkill} onDone={onDone} />
        </div>
      </details>

      <h2>Review depth</h2>
      <p className="muted sm">
        How deep each level goes. Deep is a thorough, Devin-style analysis. All three run the skill
        above.
      </p>
      {["quick", "standard", "deep"].map((lv) => (
        <DepthEditor key={lv} token={d.token} level={lv} d={d.depths[lv]} onDone={onDone} />
      ))}

      <h2>How each skill scores</h2>
      {d.stats.length === 0 ? (
        <div className="empty">
          <span className="ic">🧭</span>
          <b>No scores yet</b>
          Post a few reviews and each skill's kept-rate will show up here.
        </div>
      ) : (
        <div className="list">
          {d.stats.map((s) => (
            <div className="row" key={s.skill}>
              <div className="rowlink">
                <div className="rowtop">
                  <span className="ttl">
                    {s.skill === "global" ? "Team default" : `${s.skill}'s skill`}
                    {s.skill === d.user && <span className="tag-on" style={{ marginLeft: 6 }}>you</span>}
                  </span>
                  <span className="num" style={{ WebkitTextFillColor: "var(--fg)" }}>
                    {s.rate}% kept
                  </span>
                </div>
                <div className="ratebar">
                  <div className="ratefill" style={{ width: `${s.rate}%` }} />
                </div>
                <div className="muted sm" style={{ marginTop: 6 }}>
                  {s.kept} kept · {s.edited} reworded · {s.dropped} dropped · {s.total} findings
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
