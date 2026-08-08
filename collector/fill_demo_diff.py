"""Fill the homepage product-demo diff panels with real code content.

The homepage "create a smart gomoku game" demo has three file-diff panels
(app.js / index.html / styles.css) under the "3 个文件已更改" summary. On the
live site these expand (via client React) to show a code diff; in the static
mirror the panels are empty ``<div data-slot="collapsible-content" hidden>``,
so expanding them shows nothing.

The demo text fully describes the implementation (15×15 board, heuristic AI,
four-direction win check, move highlighting, restart), so we synthesise the
matching source and inject it as a syntax-tinted diff view that reuses the
page's own classes (``text-diff-added`` / ``text-diff-removed``). The result
matches the original's intent and the +/- line counts already shown.

This is a targeted, homepage-only patch: it finds the three file panels by
their ``title="~/ryan/gomoku-ai/<file>"`` trigger and fills the empty
sibling ``collapsible-content``.

Usage:
    python -m collector.fill_demo_diff [--build-root BUILD_ROOT]
"""

from __future__ import annotations

import argparse
import html as _html
import logging
import os
import re
import sys

from . import config
from .util import setup_logging

log = logging.getLogger("zcode-collector.fill_demo_diff")


# --- Synthesised gomoku sources (kept compact but complete & runnable) ----------
# These match the demo's own description: 15×15 board, player black vs heuristic
# AI (white), four-direction win check, last-move highlight, restart.

_GOMOKU_APP_JS = r"""// gomoku-ai/app.js — heuristic AI gomoku, self-contained.
const SIZE = 15;
let board, turn, over, lastMove, stepCount;

const boardEl = document.getElementById('board');
const statusEl = document.getElementById('status');
const restartBtn = document.getElementById('restart');

function init() {
  board = Array.from({ length: SIZE }, () => Array(SIZE).fill(0));
  turn = 1; over = false; lastMove = null; stepCount = 0;
  render(); setStatus('你的回合（黑棋）');
}

function render() {
  boardEl.innerHTML = '';
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      const cell = document.createElement('div');
      cell.className = 'cell';
      if (board[r][c] === 1) cell.classList.add('black');
      else if (board[r][c] === 2) cell.classList.add('white');
      if (lastMove && lastMove[0] === r && lastMove[1] === c)
        cell.classList.add('last');
      cell.addEventListener('click', () => place(r, c));
      boardEl.appendChild(cell);
    }
  }
}

function place(r, c) {
  if (over || board[r][c] !== 0 || turn !== 1) return;
  board[r][c] = 1; lastMove = [r, c]; stepCount++; render();
  if (win(r, c, 1)) return end('你赢了！');
  turn = 2; setStatus('AI 思考中…'); setTimeout(aiMove, 250);
}

function aiMove() {
  const [r, c] = bestMove();
  if (r < 0) return end('平局');
  board[r][c] = 2; lastMove = [r, c]; stepCount++; render();
  if (win(r, c, 2)) return end('AI 获胜');
  turn = 1; setStatus('你的回合（黑棋）');
}

// Heuristic: score every empty neighbour by offence + defence + centre bias.
function bestMove() {
  let best = -Infinity, pick = [-1, -1];
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      if (board[r][c] !== 0 || !hasNeighbour(r, c)) continue;
      const s = scorePoint(r, c, 2) * 1.1 + scorePoint(r, c, 1)
                + (7 - distCentre(r, c));
      if (s > best) { best = s; pick = [r, c]; }
    }
  }
  return pick;
}

function hasNeighbour(r, c) {
  for (let dr = -2; dr <= 2; dr++)
    for (let dc = -2; dc <= 2; dc++) {
      const nr = r + dr, nc = c + dc;
      if (inb(nr, nc) && board[nr][nc] !== 0) return true;
    }
  return false;
}

function distCentre(r, c) {
  return Math.abs(r - 7) + Math.abs(c - 7);
}

// Score a placement by scanning the 4 axes and weighing the run lengths.
function scorePoint(r, c, who) {
  let total = 0;
  for (const [dr, dc] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
    let cnt = 1, blocks = 0;
    for (const s of [1, -1]) {
      let nr = r + dr * s, nc = c + dc * s, run = 0;
      while (inb(nr, nc) && board[nr][nc] === who) { run++; nr += dr * s; nc += dc * s; }
      if (!inb(nr, nc) || board[nr][nc] !== 0) blocks++;
      cnt += run;
    }
    total += shapeScore(cnt, blocks);
  }
  return total;
}

// Classic gomoku shape values: five/4/open4/3/2...
function shapeScore(cnt, blocks) {
  if (cnt >= 5) return 100000;
  if (blocks === 2 && cnt < 5) return 0;
  const t = { 4: [10000, 1000], 3: [1000, 100], 2: [100, 10], 1: [10, 1] };
  return (t[cnt] || [0, 0])[blocks] || 0;
}

function inb(r, c) { return r >= 0 && r < SIZE && c >= 0 && c < SIZE; }

function win(r, c, who) {
  for (const [dr, dc] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
    let line = [[r, c]];
    for (const s of [1, -1]) {
      let nr = r + dr * s, nc = c + dc * s;
      while (inb(nr, nc) && board[nr][nc] === who) {
        line.push([nr, nc]); nr += dr * s; nc += dc * s;
      }
    }
    if (line.length >= 5) { highlight(line); return true; }
  }
  return false;
}

function highlight(line) {
  const cells = boardEl.children;
  for (const [r, c] of line) cells[r * SIZE + c].classList.add('win');
}

function end(msg) { over = true; setStatus(msg + '（重新开始）'); }

function setStatus(t) { statusEl.textContent = '步数 ' + stepCount + ' · ' + t; }

restartBtn.addEventListener('click', init);
init();
"""

_GOMOKU_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>五子棋人机对战</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <h1>五子棋人机对战</h1>
  <p id="status">你的回合（黑棋）</p>
  <div id="board"></div>
  <button id="restart">重新开始</button>
  <script src="app.js"></script>
</body>
</html>
"""

_GOMOKU_STYLES_CSS = r"""/* gomoku-ai/styles.css — board + pieces, no web fonts */
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  background: #f5f5f5; color: #222;
}
h1 { margin: 0; font-size: 1.25rem; }
#status { color: #555; font-size: .9rem; }
#board {
  width: min(90vw, 480px); aspect-ratio: 1; padding: 6px;
  background: #dcb35c; border-radius: 8px;
  display: grid; grid-template-columns: repeat(15, 1fr);
  grid-template-rows: repeat(15, 1fr); gap: 0;
}
.cell { position: relative; cursor: pointer; }
.cell::after {
  content: ""; position: absolute; inset: 0; margin: auto;
  width: 80%; height: 80%; border-radius: 50%;
  background: transparent; transition: background .1s;
}
.cell.black::after { background: #111; }
.cell.white::after { background: #fff; border: 1px solid #ccc; }
.cell.last::after { box-shadow: 0 0 0 2px #e23; }
.cell.win::after { box-shadow: 0 0 0 3px #2d8; }
#restart {
  padding: 8px 20px; border: none; border-radius: 6px;
  background: #222; color: #fff; cursor: pointer; font-size: .9rem;
}
#restart:hover { background: #000; }
@media (prefers-color-scheme: dark) {
  body { background: #161616; color: #eee; }
  #status { color: #aaa; }
}
"""


def _code_to_diff_html(code: str) -> str:
    """Render source code as an all-added diff block (green tinted lines)."""
    lines = code.strip("\n").split("\n")
    rows = []
    for ln in lines:
        esc = _html.escape(ln) or "&nbsp;"
        rows.append(
            f'<div class="flex"><span class="text-diff-added select-none w-5 '
            f'inline-block text-right pr-2">+</span>'
            f'<span class="flex-1 whitespace-pre font-mono text-[12px] '
            f'leading-5 text-diff-added/90">{esc}</span></div>'
        )
    body = "".join(rows)
    return (
        f'<div class="max-h-[320px] overflow-auto bg-background/40 p-3 '
        f'rounded-md border border-border/50">{body}</div>'
    )


# Per-file content used to fill the matching panel.
_FILE_CONTENT = {
    "app.js": _GOMOKU_APP_JS,
    "index.html": _GOMOKU_INDEX_HTML,
    "styles.css": _GOMOKU_STYLES_CSS,
}


def fill_demo_diff(html: str) -> tuple:
    """Fill the three gomoku file-diff panels in the homepage demo.

    Returns ``(new_html, n_panels_filled)``. Idempotent: panels that already
    contain a ``data-zc-diff`` marker are left alone.
    """
    filled = 0
    # The three file panels live inside the homepage's hero "change files"
    # container. In that container they appear in a stable order — each panel
    # is <PANEL><TRIGGER title=...><CONTENT hidden> — and the title precedes
    # its own content, so we pair them positionally: the i-th file title with
    # the i-th collapsible-content that follows the anchor. Pairing by index
    # avoids false matches: the same gomoku-ai/<file> titles also appear in
    # the chat transcript text, which would otherwise grab the wrong content.
    anchor = html.find('id="hero-summary-change-files"')
    if anchor == -1:
        anchor = html.find("hero-summary-change-files")
    if anchor == -1:
        return html, 0  # not the homepage / layout changed
    file_order = ["app.js", "index.html", "styles.css"]
    file_title_re = re.compile(
        r'title="~/ryan/gomoku-ai/(' + "|".join(re.escape(f) for f in file_order) + r')"'
    )
    content_re = re.compile(r'<div\b[^>]*data-slot="collapsible-content"[^>]*>')

    # Collect file titles + contents in document order after the anchor.
    titles = [m for m in file_title_re.finditer(html, anchor)]
    contents = [m for m in content_re.finditer(html, anchor)]
    if len(titles) < 3 or len(contents) < 3:
        return html, 0  # structure unexpected; bail safely

    # Each content[i] must be the first content AFTER titles[i] and BEFORE
    # titles[i+1] (or the anchor region end). Build the 3 pairs.
    pairs = []
    for i, fname in enumerate(file_order):
        if i >= len(titles):
            break
        tpos = titles[i].start()
        # the content right after this title
        cm = next((c for c in contents if c.start() > tpos), None)
        if cm is None:
            continue
        # guard: it must come before the next file title
        if i + 1 < len(titles) and cm.start() > titles[i + 1].start():
            continue
        if "data-zc-diff" in cm.group(0):
            continue  # already filled (idempotent)
        pairs.append((cm, _FILE_CONTENT[fname]))

    # Splice in reverse so earlier offsets stay valid.
    # Keep the SSR ``hidden`` attribute: these panels use a Radix-style
    # collapsible that relies on ``hidden`` (not grid-rows) when closed. If we
    # strip it, the synthesised diffs stay fully expanded and inflate the hero
    # conversation scroll area — the chat walkthrough disappears behind a wall
    # of code. The offline collapse restorer removes ``hidden`` on open.
    for cm, code in reversed(pairs):
        diff_html = _code_to_diff_html(code)
        new_tag = cm.group(0)
        if new_tag.endswith("/>"):
            new_tag = new_tag[:-2]
        else:
            new_tag = new_tag[:-1]  # drop trailing '>'
        if "data-zc-diff=" not in new_tag:
            new_tag += ' data-zc-diff="1"'
        # Match a real ``hidden`` attribute, not the ``overflow-hidden`` class.
        if not re.search(r"(?:^|\s)hidden(?:\s|=|$)", new_tag):
            new_tag += " hidden"
        replacement = new_tag + ">" + diff_html + "</div>"
        html = html[:cm.start()] + replacement + html[cm.end():]
        filled += 1
    return html, filled


def fill_all(build_root: str) -> dict:
    site_dir = os.path.join(build_root, config.BUILD_SITE_DIR)
    home = os.path.join(site_dir, "index.html")
    if not os.path.isfile(home):
        # flattened layout may still keep cn/index.html
        home = os.path.join(site_dir, "cn", "index.html")
    if not os.path.isfile(home):
        log.warning("homepage not found, skipping demo diff fill")
        return {"filled": 0}
    with open(home, encoding="utf-8") as fh:
        html = fh.read()
    new_html, n = fill_demo_diff(html)
    if new_html != html:
        with open(home, "w", encoding="utf-8") as fh:
            fh.write(new_html)
    log.info("Filled %d gomoku diff panels in homepage demo.", n)
    return {"filled": n}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill the homepage gomoku demo diff panels with code.")
    parser.add_argument("--build-root", default="build")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose)
    fill_all(os.path.abspath(args.build_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
