"""Diff-anchor helpers shared by the review bot.

GitHub rejects an ENTIRE review with 422 if any single inline comment points at a line
outside the diff — so one bad line number would lose every finding. Anchors are validated
at POST time (not review time) because the PR may have gained commits while the review sat
in the dashboard.
"""
import re

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(patch):
    """RIGHT-side line numbers a review comment can anchor to.

    Context and added lines exist in the new file and are valid targets; removed lines are
    not (they only exist on the LEFT side).
    """
    lines, new_no = set(), 0
    for raw in (patch or "").split("\n"):
        m = HUNK.match(raw)
        if m:
            new_no = int(m.group(1))
            continue
        if not raw:
            continue
        head = raw[0]
        if head in "+ ":
            lines.add(new_no)
            new_no += 1
        # '-' consumes no new-file line; '\' (no-newline marker) is ignored
    return lines


def anchor_map(files):
    """{filename: {commentable line numbers}} from the PR files API payload."""
    return {f["filename"]: commentable_lines(f.get("patch")) for f in files}


def split_anchorable(comments, anchors):
    """Partition comments into (anchorable, orphans) against the current diff."""
    inline, orphans = [], []
    for c in comments:
        path, line = c.get("path"), c.get("line")
        body = (c.get("body") or "").strip()
        if not body:
            continue
        if path and isinstance(line, int) and line in anchors.get(path, set()):
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": body})
        else:
            orphans.append(c)
    return inline, orphans


def orphan_block(orphans):
    """Fold un-anchorable findings into the summary body so they still reach the human."""
    if not orphans:
        return ""
    rows = []
    for c in orphans:
        line, path = c.get("line"), c.get("path") or "?"
        loc = f"{path}:{line}" if isinstance(line, int) else path
        sev = c.get("severity")
        prefix = f"**{sev}** — " if sev else ""
        rows.append(f"- **`{loc}`** — {prefix}{(c.get('body') or '').strip()}")
    return ("\n\n<details><summary>"
            f"{len(orphans)} finding(s) that could not be anchored to a diff line"
            "</summary>\n\n" + "\n".join(rows) + "\n\n</details>")
