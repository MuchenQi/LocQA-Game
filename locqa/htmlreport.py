"""The HTML proof sheet.

Most findings read perfectly well as a line of text. A few do not: a string
overrunning its box and a line break stranding a 。 at a line head are
*spatial* facts, and a reviewer handed the number "28 half-width units against
a budget of 24" has to imagine the overflow before they can judge it.

Those findings render on a 原稿用紙 character grid — one cell per full-width
character, the budget drawn as a rule, cells past the line budget in
correction red. The reviewer sees the overrun in the same terms the engine
will, which is the same reason a print proof is marked up on the proof and not
described in a covering letter.
"""

from __future__ import annotations

import html
from datetime import datetime

from .checks.base import Config
from .checks.integrity import layout_text
from .model import (
    Finding, Script, Severity, display_width, normalize_breaks, wrap_by_width,
)
from .report import summarize

# Checks whose findings are about individual characters and their position,
# and so are worth drawing rather than describing.
GRID_CHECKS = frozenset({
    "length.budget", "ja.kinsoku", "ja.charset", "ja.punctuation",
    "ja.wave_dash", "ja.spacing", "ja.number_width", "placeholder.corrupt",
})

# Cell geometry, matched to the stylesheet: a full-width cell occupies 27px
# and overlaps its neighbour's border by 1px, so each advances 26px and each
# half-width unit is 13px. The budget rule is placed in those units.
_UNIT_PX = 13
_ORIGIN_PX = 1

STYLE = """
  :root {
    --paper:    #FAF9F6;
    --sumi:     #16222E;
    --ai:       #2F4E6B;
    --ai-wash:  #DDE5EB;
    --rule:     #C9D4DD;
    --shu:      #BE3A2B;
    --shu-wash: #F8E4E0;
    --midori:   #3A6B52;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, monospace;
    --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Kaku Gothic ProN", sans-serif;
    --mincho: "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", "MS Mincho", serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--sumi);
    font-family: var(--sans); font-size: 15px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 940px; margin: 0 auto; padding: 0 24px 96px; }

  /* ---- masthead ---- */
  header { padding: 56px 0 0; }
  .kanji {
    font-family: var(--mincho); font-size: 13px; letter-spacing: .5em;
    color: var(--shu); display: block; margin-bottom: 18px;
  }
  h1 {
    font-size: clamp(30px, 6vw, 46px); line-height: 1.05; margin: 0 0 6px;
    font-weight: 600; letter-spacing: -0.02em;
  }
  .meta { font-family: var(--mono); font-size: 12.5px; color: var(--ai); }
  .rule { height: 2px; background: var(--shu); margin: 28px 0 0; }
  .rule.thin { height: 1px; background: var(--rule); margin: 0; }

  /* ---- tally ---- */
  .tally { display: flex; flex-wrap: wrap; gap: 0; margin: 0 0 40px;
           border-bottom: 1px solid var(--rule); }
  .tally button {
    appearance: none; background: none; border: 0; cursor: pointer;
    padding: 18px 22px 16px 0; margin-right: 22px; text-align: left;
    font-family: inherit; color: inherit; border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }
  .tally button[aria-pressed="false"] { opacity: .32; }
  .tally button[aria-pressed="true"].sev-blocker { border-bottom-color: var(--shu); }
  .tally button[aria-pressed="true"].sev-major   { border-bottom-color: var(--ai); }
  .tally button[aria-pressed="true"].sev-minor,
  .tally button[aria-pressed="true"].sev-info    { border-bottom-color: var(--rule); }
  .tally .n { display: block; font-size: 30px; font-weight: 600; line-height: 1;
              font-variant-numeric: tabular-nums; }
  .tally .l { font-family: var(--mono); font-size: 11px; text-transform: uppercase;
              letter-spacing: .12em; color: var(--ai); }
  .sev-blocker .n { color: var(--shu); }
  .clean { color: var(--midori); font-family: var(--mono); font-size: 13px; }

  /* ---- findings ---- */
  .band { font-family: var(--mono); font-size: 11px; letter-spacing: .16em;
          text-transform: uppercase; color: var(--ai); margin: 44px 0 0;
          padding-bottom: 8px; border-bottom: 1px solid var(--rule); }
  .band.hidden, .f.hidden { display: none; }
  .f { padding: 20px 0 22px; border-bottom: 1px solid var(--rule); }
  .f-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px;
            font-family: var(--mono); font-size: 11.5px; }
  .id { color: var(--sumi); font-weight: 600; }
  .check { color: var(--ai); }
  .reach { color: var(--ai); opacity: .7; margin-left: auto; }
  .sev-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .sev-dot.blocker { background: var(--shu); }
  .sev-dot.major   { background: var(--ai); }
  .sev-dot.minor   { background: var(--rule); }
  .sev-dot.info    { background: var(--rule); opacity: .5; }
  .msg { margin: 9px 0 0; max-width: 68ch; }
  .speaker { font-family: var(--mono); font-size: 11px; color: var(--ai);
             text-transform: uppercase; letter-spacing: .1em; }
  .src { font-size: 13.5px; color: var(--ai); margin: 10px 0 0; max-width: 68ch; }
  .src::before { content: "EN "; font-family: var(--mono); font-size: 10px;
                 letter-spacing: .1em; opacity: .6; }
  .excerpt { font-family: var(--mincho); font-size: 17px; margin: 8px 0 0;
             max-width: 68ch; }
  .fix { margin: 10px 0 0; font-size: 13.5px; color: var(--ai);
         padding-left: 14px; border-left: 2px solid var(--shu-wash); max-width: 68ch; }

  /* ---- signature: genkō-yōshi grid ---- */
  .grid { margin: 14px 0 2px; overflow-x: auto; padding-bottom: 4px; }
  .grow { display: flex; align-items: stretch; margin-bottom: 3px; }
  .gnum { font-family: var(--mono); font-size: 10px; color: var(--ai);
          width: 20px; flex: 0 0 20px; display: flex; align-items: center;
          justify-content: flex-end; padding-right: 7px; opacity: .55; }
  .cells { display: flex; position: relative; }
  .c {
    width: 27px; height: 27px; flex: 0 0 27px;
    border: 1px solid var(--ai-wash); margin-left: -1px;
    display: flex; align-items: center; justify-content: center;
    font-family: var(--mincho); font-size: 16px; line-height: 1;
    background: rgba(255,255,255,.6);
  }
  .c.half { width: 14px; flex-basis: 14px; font-size: 13px; }
  .c.over { background: var(--shu-wash); border-color: #E9BDB6; }
  .c.flag { background: var(--shu); border-color: var(--shu); color: #fff; }
  .budget { position: absolute; top: -3px; bottom: -3px; width: 2px;
            background: var(--shu); opacity: .85; }
  .budget::after {
    content: attr(data-label); position: absolute; top: -15px; left: 4px;
    font-family: var(--mono); font-size: 9.5px; letter-spacing: .06em;
    color: var(--shu); white-space: nowrap;
  }
  .gw { font-family: var(--mono); font-size: 10px; color: var(--ai);
        display: flex; align-items: center; padding-left: 10px; opacity: .6;
        white-space: nowrap; }

  /* ---- by-check footer ---- */
  .bycheck { margin-top: 52px; }
  .bycheck table { width: 100%; border-collapse: collapse; font-family: var(--mono);
                   font-size: 12px; }
  .bycheck td { padding: 7px 0; border-bottom: 1px solid var(--rule); }
  .bycheck td:first-child { width: 46px; text-align: right; padding-right: 16px;
                            font-variant-numeric: tabular-nums; }
  .bycheck td:last-child { color: var(--ai); }
  footer { margin-top: 44px; font-family: var(--mono); font-size: 11px; color: var(--ai);
           opacity: .7; }

  :focus-visible { outline: 2px solid var(--shu); outline-offset: 3px; }
  @media (prefers-reduced-motion: no-preference) {
    .f { transition: opacity .12s ease; }
  }
  @media (max-width: 560px) {
    .wrap { padding: 0 16px 64px; }
    .tally button { padding-right: 14px; margin-right: 14px; }
    .reach { margin-left: 0; }
  }
"""

SCRIPT = """
  // Severity filters. Each tally button toggles its band; the counts stay
  // truthful because filtering hides rows rather than recomputing totals.
  const buttons = document.querySelectorAll('.tally button');
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      const on = btn.getAttribute('aria-pressed') === 'true';
      btn.setAttribute('aria-pressed', on ? 'false' : 'true');
      const sev = btn.dataset.sev;
      document.querySelectorAll('.f[data-sev="' + sev + '"]').forEach(function (el) {
        el.classList.toggle('hidden', on);
      });
      const band = document.querySelector('.band[data-band="' + sev + '"]');
      if (band) band.classList.toggle('hidden', on);
    });
  });
"""


def _flagged_characters(finding: Finding) -> set[str]:
    """The characters this finding is actually about, for marking cells."""
    marked: set[str] = set(finding.meta.get("characters", []) or [])
    char = finding.meta.get("char")
    if char:
        marked.add(char)
    codepoint = finding.meta.get("codepoint")
    if codepoint:
        try:
            marked.add(chr(int(str(codepoint).removeprefix("U+"), 16)))
        except ValueError:
            pass
    return marked


def _grid(script: Script, finding: Finding, config: Config) -> str:
    """Render the target on a character grid, with the budget drawn as a rule."""
    segment = script.segments.get(finding.segment_id or "")
    if not segment or not segment.target.strip():
        return ""

    width = segment.max_width or config.default_max_width

    # Kinsoku already wrapped the string to decide there was a violation;
    # reusing its own rows means the proof shows what the check saw rather
    # than a second, possibly different, wrap of the same text.
    rows = finding.meta.get("wrapped")
    if not rows:
        expanded = normalize_breaks(
            layout_text(
                segment.target, config.placeholder_widths, config.default_placeholder_width,
                also=segment.source,
            )
        )
        rows = (
            wrap_by_width(expanded, width, config.ambiguous_wide)
            if width
            else expanded.split("\n")
        )
    if not rows:
        return ""

    marked = _flagged_characters(finding)
    target_row = finding.meta.get("line")
    budget_lines = segment.max_lines or (1 if width else len(rows))

    out = ['    <div class="grid">']
    for index, row in enumerate(rows, 1):
        cells = []
        for position, char in enumerate(row):
            half = "half" if display_width(char, config.ambiguous_wide) == 1 else ""
            state = "ok"
            if index > budget_lines:
                # Past the line budget: these cells do not fit in the box.
                state = "over"
            if char in marked and (target_row is None or target_row == index):
                # Kinsoku violations are positional — only the stranded cell
                # at a line head or line end is the bug, not every 。 in the row.
                if target_row is None or position in (0, len(row) - 1):
                    state = "flag"
            cells.append(
                f'<span class="c {half} {state}">{html.escape(char)}</span>'
            )

        measured = display_width(row, config.ambiguous_wide)
        rule = ""
        if width:
            left = width * _UNIT_PX + _ORIGIN_PX
            rule = (
                f'\n          <span class="budget" data-label="{width}"'
                f'\n            style="left: {left}px"></span>'
            )
        out.append('      <div class="grow">')
        out.append(f'        <span class="gnum">{index}</span>')
        out.append('        <div class="cells">')
        out.append("          " + "".join(cells) + rule)
        out.append("        </div>")
        out.append(
            f'        <span class="gw">{measured}'
            + (f"/{width}" if width else "")
            + "</span>"
        )
        out.append("      </div>")
    out.append("    </div>")
    return "\n".join(out)


def _finding_html(script: Script, finding: Finding, config: Config) -> str:
    segment = script.segments.get(finding.segment_id or "")
    severity = finding.severity.label.lower()
    where = finding.segment_id or finding.node_id or "—"

    parts = [f'  <article class="f" data-sev="{severity}">']
    parts.append('    <div class="f-head">')
    parts.append(f'      <span class="sev-dot {severity}"></span>')
    parts.append(f'      <span class="id">{html.escape(where)}</span>')
    parts.append(f'      <span class="check">{html.escape(finding.check_id)}</span>')
    if segment and segment.speaker:
        parts.append(f'      <span class="speaker">{html.escape(segment.speaker)}</span>')
    if finding.reach < 1.0:
        parts.append(f'      <span class="reach">reach {finding.reach:.2f}</span>')
    parts.append("    </div>")
    parts.append(f'    <p class="msg">{html.escape(finding.message)}</p>')

    if segment and segment.source:
        parts.append(f'    <p class="src">{html.escape(segment.source)}</p>')

    grid = _grid(script, finding, config) if finding.check_id in GRID_CHECKS else ""
    if grid:
        parts.append(grid)
    elif finding.excerpt:
        parts.append(f'    <p class="excerpt">{html.escape(finding.excerpt)}</p>')

    if finding.suggestion:
        parts.append(f'    <p class="fix">{html.escape(finding.suggestion)}</p>')
    parts.append("  </article>")
    return "\n".join(parts)


def to_html(
    script: Script,
    findings: list[Finding],
    config: Config | None = None,
    checks_run: int | None = None,
) -> str:
    """Render a full standalone proof sheet. No external assets, no network."""
    config = config or Config()
    counts = summarize(findings)
    from . import __version__

    out = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(script.name)} — locqa proof</title>",
        "<style>" + STYLE + "</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        "  <header>",
        '    <span class="kanji">校正刷</span>',
        f"    <h1>{html.escape(script.name)}</h1>",
        '    <div class="meta">',
        f"      {html.escape(script.source_format or 'script')} &middot; "
        f"{html.escape(script.source_lang)}&thinsp;→&thinsp;{html.escape(script.target_lang)}"
        f" &middot; {len(script)} segments"
        f" &middot; {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "    </div>",
        '    <div class="rule"></div>',
        "  </header>",
    ]

    present = [s for s in reversed(list(Severity)) if counts["by_severity"].get(s.label)]
    if findings:
        out.append('  <div class="tally">')
        for severity in present:
            key = severity.label.lower()
            out.append(f'    <button class="sev-{key}" aria-pressed="true" data-sev="{key}">')
            out.append(f'      <span class="n">{counts["by_severity"][severity.label]}</span>')
            out.append(f'      <span class="l">{severity.label}</span>')
            out.append("    </button>")
        out.append("  </div>")
    else:
        out.append('  <p class="clean">No findings.</p>')

    for severity in present:
        key = severity.label.lower()
        hits = [f for f in findings if f.severity is severity]
        out.append(
            f'  <div class="band" data-band="{key}">{severity.label}'
            f" &nbsp;·&nbsp; {len(hits)}</div>"
        )
        for finding in hits:
            out.append(_finding_html(script, finding, config))

    if findings:
        out.append('  <div class="bycheck">')
        out.append('    <div class="band">By check</div>')
        out.append("    <table>")
        for check_id, count in counts["by_check"].items():
            out.append(f"      <tr><td>{count}</td><td>{html.escape(check_id)}</td></tr>")
        out.append("    </table>")
        out.append("  </div>")

    ran = f" — {checks_run} checks run" if checks_run else ""
    out.append(f"  <footer>locqa {__version__}{ran}</footer>")
    out.append("</div>")
    out.append("<script>" + SCRIPT + "</script>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"
