"""Small markdown renderer for the review dashboard.

Deliberately dependency-free — the box has no pip packages and the endpoint should not need
any. Covers what the pr-review skill actually emits: headings, fenced code, tables, ordered
and unordered lists, blockquotes, inline code, bold/italic and links.
"""
import html
import re

FENCE = re.compile(r"```(\w*)\n(.*?)```", re.S)
CODE = re.compile(r"`([^`\n]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
OLI = re.compile(r"^\s*\d+[.)]\s+(.*)$")
ULI = re.compile(r"^\s*[-*]\s+(.*)$")


def inline(s):
    """Escape, then apply inline markup. Escaping first keeps user text inert."""
    s = html.escape(s)
    s = LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = CODE.sub(r"<code>\1</code>", s)
    s = BOLD.sub(r"<strong>\1</strong>", s)
    s = ITAL.sub(r"<em>\1</em>", s)
    return s


def _is_table(lines):
    return (len(lines) >= 2 and lines[0].lstrip().startswith("|")
            and set(lines[1].replace("|", "").replace(" ", "")) <= set("-:")
            and "-" in lines[1])


def _cells(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def _table(lines):
    head = "".join(f"<th>{inline(c)}</th>" for c in _cells(lines[0]))
    body = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in _cells(r)) + "</tr>"
        for r in lines[2:] if r.strip())
    return f"<div class=tw><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _list(lines, ordered):
    pat = OLI if ordered else ULI
    items = []
    for ln in lines:
        m = pat.match(ln)
        if m:
            items.append(f"<li>{inline(m.group(1))}</li>")
        elif items:  # continuation line folds into the previous item
            items[-1] = items[-1][:-5] + " " + inline(ln.strip()) + "</li>"
    tag = "ol" if ordered else "ul"
    return f"<{tag}>{''.join(items)}</{tag}>"


def render(text):
    if not text:
        return ""
    stash = []

    def keep(m):
        lang = m.group(1) or ""
        stash.append(f'<pre data-lang="{html.escape(lang)}"><code>'
                     f"{html.escape(m.group(2))}</code></pre>")
        return f"\x00{len(stash) - 1}\x00"

    text = FENCE.sub(keep, text.replace("\r\n", "\n"))
    out = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and re.fullmatch(r"\x00\d+\x00", lines[0].strip()):
            out.append(lines[0].strip())
            continue
        m = HEAD.match(lines[0])
        if m:
            lvl = min(len(m.group(1)) + 2, 6)   # ## in content -> <h4>, never competes with page h1/h2
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            lines = lines[1:]
            if not lines:
                continue
        if _is_table(lines):
            out.append(_table(lines))
        elif all(OLI.match(ln) or not ln.strip() for ln in lines):
            out.append(_list(lines, True))
        elif lines[0].lstrip().startswith(("- ", "* ")):
            out.append(_list(lines, False))
        elif lines[0].lstrip().startswith(">"):
            inner = " ".join(ln.lstrip("> ").strip() for ln in lines)
            out.append(f"<blockquote>{inline(inner)}</blockquote>")
        else:
            out.append("<p>" + inline("\n".join(lines)).replace("\n", "<br>") + "</p>")
    rendered = "\n".join(out)
    for i, blk in enumerate(stash):
        rendered = rendered.replace(f"\x00{i}\x00", blk)
    return rendered
