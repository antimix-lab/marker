"""Reconstruct a table's HTML from the PDF text layer (pdftext), CPU-only.

Ported from the datalab pdftext_backfill task, trimmed to the deterministic,
per-block core (no cross-page cluster consensus, no LLM orphan/header/garble
calls — those reduce to their documented deterministic fallbacks). Given the
pdftext spans inside a Table block's bbox, it sweeps several grid
parameterizations, scores them with a deterministic judge, and emits
``<table>`` HTML. Returns ``(html, score)`` or ``None`` when no grid resolves.

Input ``lines``: ``[(spans, y0, y1), ...]`` with ``spans = [(text, x0, x1), ...]``
in PDF points, matching the shape pdftext's dictionary_output yields per line.
"""

import itertools
import re
from collections import Counter, defaultdict
from html import escape
from statistics import median

MIN_CELLS_PER_ROW = 2  # a "data row" has >= this many tokens (2 supports two-column tables; the judge filters degenerate grids)
MIN_TABLE_ROWS = 3  # need at least this many data rows to treat it as a table
GAP_MIN_PT = 2.5  # projection: gaps narrower than this don't separate columns
PROJ_FRACS = (0.01, 0.03, 0.10)  # projection row-frequency thresholds to sweep
GRID_REGULARITY = 0.5

_PUA = re.compile("[\ue000-\uf8ff\ufffd]")  # private-use area + replacement char
_GARBLE_CHAR_FRAC = 0.02  # >2% private-use / replacement chars => garbled
# Leader runs (TOC dot leaders, form underscores/dashes) - not real cells;
# gridding them as columns explodes the column count, so they're dropped.
_LEADER_ONLY = re.compile(r"^[.·•…_\-\s]+$")
_NUMISH = re.compile(r"^-?[\d,.]*\d[\d,.]*%?$")
_NUM_TOK = re.compile(r"-?[\d,]+(\.\d+)?%?")
_YEAR = re.compile(r"^(19|20)\d\d$")
_PCTISH = re.compile(r"(less than|to less|%)")


# --------------------------------------------------------------------------- #
# garble gate (deterministic prefilter only)
# --------------------------------------------------------------------------- #
def _garble_ok(lines) -> bool:
    """True if the text layer looks clean. Deterministic: reject only on a high
    fraction of private-use / replacement glyphs (mojibake / bad encoding)."""
    text = " ".join(t for spans, _, _ in lines[:40] for t, _, _ in spans)
    if not text:
        return False
    return len(_PUA.findall(text)) / len(text) <= _GARBLE_CHAR_FRAC


def _coalesce_same_baseline(lines):
    """Merge cells emitted by pdftext as separate lines at the same y."""
    merged = []
    for spans, y0, y1 in sorted(lines, key=lambda item: (item[1], item[2])):
        if merged and abs(merged[-1][1] - y0) <= 1.5:
            merged[-1][0].extend(spans)
            merged[-1][0].sort(key=lambda span: span[1])
            merged[-1][2] = max(merged[-1][2], y1)
        else:
            merged.append([list(spans), y0, y1])
    return [(spans, y0, y1) for spans, y0, y1 in merged]


# --------------------------------------------------------------------------- #
# header band split
# --------------------------------------------------------------------------- #
def _numfrac(spans) -> float:
    return sum(1 for t, _, _ in spans if _NUMISH.match(t)) / max(len(spans), 1)


def _find_header_band(lines):
    """y of the first DATA line (span-rich AND numeric-majority); everything
    above is the header band. None when the table has no numeric transition."""
    ys = [
        y0
        for spans, y0, _ in lines
        # Strictly more numeric than text: a 2-token (title, page#) TOC row is
        # exactly 0.5 and must not anchor the data region, or everything above
        # it gets swallowed into the header band.
        if len(spans) >= MIN_CELLS_PER_ROW and _numfrac(spans) > 0.5
    ]
    return min(ys) if ys else None


# --------------------------------------------------------------------------- #
# grid candidates
# --------------------------------------------------------------------------- #
def _grid_span(data_rows, bucket: str):
    """Span-structure grid: K = modal span count; cuts = midpoints of the
    per-column median x0 over exact-K anchor rows."""
    if len(data_rows) < MIN_TABLE_ROWS:
        return None
    k = Counter(len(r) for r in data_rows).most_common(1)[0][0]
    if k < 2:
        return None
    anchors = [r for r in data_rows if len(r) == k]
    if len(anchors) < 3:
        return None
    col_x = [median(a[j][1] for a in anchors) for j in range(k)]
    cut_xs = [(col_x[j] + col_x[j + 1]) / 2 for j in range(k - 1)]

    grid, counts = [], []
    for row in data_rows:
        cells, cnts = [""] * k, [0] * k
        for t, x0, x1 in row:
            pos = x0 if bucket == "x0" else (x0 + x1) / 2
            c = min(sum(1 for cx in cut_xs if pos >= cx), k - 1)
            cells[c] = f"{cells[c]} {t}".strip() if cells[c] else t
            cnts[c] += 1
        grid.append(cells)
        counts.append(cnts)
    return k, cut_xs, grid, counts


def _grid_proj(data_rows, frac: float):
    """Whitespace-gap projection grid: an x position belongs to a column iff
    covered by a span in > frac of rows."""
    if len(data_rows) < MIN_TABLE_ROWS:
        return None
    lo = min(x0 for r in data_rows for _, x0, _ in r)
    hi = max(x1 for r in data_rows for _, _, x1 in r)
    n = int(hi - lo) + 2
    cov = [0] * n
    for r in data_rows:
        for _, x0, x1 in r:
            for i in range(int(x0 - lo), min(int(x1 - lo) + 1, n)):
                cov[i] += 1
    thr = frac * len(data_rows)
    runs, i = [], 0
    while i < n:
        if cov[i] > thr:
            j = i
            while j < n and cov[j] > thr:
                j += 1
            runs.append((lo + i, lo + j))
            i = j
        else:
            i += 1
    if not runs:
        return None
    cols = [runs[0]]
    for c in runs[1:]:
        if c[0] - cols[-1][1] < GAP_MIN_PT:
            cols[-1] = (cols[-1][0], c[1])
        else:
            cols.append(c)
    k = len(cols)
    if k < 2:
        return None
    cut_xs = [(cols[j][1] + cols[j + 1][0]) / 2 for j in range(k - 1)]

    grid, counts = [], []
    for row in data_rows:
        cells, cnts = [""] * k, [0] * k
        for t, x0, x1 in row:
            best, bo = None, 0.0
            for j, (c0, c1) in enumerate(cols):
                o = max(0.0, min(x1, c1) - max(x0, c0))
                if o > bo:
                    best, bo = j, o
            if best is None:
                cx = (x0 + x1) / 2
                best = min(
                    range(k), key=lambda j: abs((cols[j][0] + cols[j][1]) / 2 - cx)
                )
            cells[best] = f"{cells[best]} {t}".strip() if cells[best] else t
            cnts[best] += 1
        grid.append(cells)
        counts.append(cnts)
    return k, cut_xs, grid, counts


def _candidates(data_rows) -> dict:
    out = {}
    for bucket in ("x0", "center"):
        g = _grid_span(data_rows, bucket)
        if g:
            out[f"span-{bucket}"] = g
    for frac in PROJ_FRACS:
        g = _grid_proj(data_rows, frac)
        if g:
            out[f"proj-{int(frac * 100)}"] = g
    return out


# --------------------------------------------------------------------------- #
# deterministic judge
# --------------------------------------------------------------------------- #
def _cell_class(c: str) -> str:
    c = c.strip()
    if not c:
        return "empty"
    if _YEAR.match(c):
        return "year"
    if _NUMISH.match(c):
        return "num"
    if _PCTISH.search(c):
        return "range"
    return "text"


def _score_grid(k, grid, counts, n_header_cells) -> float:
    """Composite in [0,1]: type purity, compound-cell penalty, fill,
    header-count agreement, spans-per-cell (double weight)."""
    nonempty = [c for r in grid for c in r if c.strip()]
    if not nonempty:
        return 0.0
    fill = len(nonempty) / max(k * len(grid), 1)
    compound = sum(
        1
        for c in nonempty
        if len(_NUM_TOK.findall(c)) >= 2
        and _cell_class(c) == "text"
        and sum(ch.isdigit() for ch in c) > len(c) * 0.4
    )
    no_compound = 1 - compound / len(nonempty)
    purs = []
    for j in range(k):
        cc = Counter(_cell_class(r[j]) for r in grid)
        cc.pop("empty", None)
        if cc:
            purs.append(cc.most_common(1)[0][1] / sum(cc.values()))
    purity = sum(purs) / len(purs) if purs else 0.0
    hdr = 0.5 if n_header_cells is None else max(0.0, 1 - abs(n_header_cells - k) / k)
    occ = [c for row in counts for c in row if c > 0]
    one_span = sum(1 for c in occ if c == 1) / max(len(occ), 1)
    return (purity + no_compound + fill + hdr + 2 * one_span) / 6


def _pick_winner(cands: dict, n_header_cells):
    best_name, best, best_score = None, None, -1.0
    for name, (k, cut_xs, grid, counts) in cands.items():
        s = _score_grid(k, grid, counts, n_header_cells)
        if s > best_score:
            best_name, best, best_score = name, (k, cut_xs, grid, counts), s
    return best_name, best, best_score


# --------------------------------------------------------------------------- #
# headers + wrapped lines
# --------------------------------------------------------------------------- #
def _full_intervals(cut_xs: list) -> list:
    edges = [-1e9, *cut_xs, 1e9]
    return list(itertools.pairwise(edges))


def _avg_col_width(cut_xs: list) -> float:
    widths = [b - a for a, b in itertools.pairwise(cut_xs)]
    return (sum(widths) / len(widths)) if widths else 100.0


def _stitch_band_headers(band_lines, cut_xs, k):
    """Assign header-band spans to columns by center within full-width column
    intervals; stitch top-to-bottom. Returns (names, n_named)."""
    full = _full_intervals(cut_xs)
    avg_w = _avg_col_width(cut_xs)
    parts = defaultdict(list)
    for spans, _y0 in sorted(band_lines, key=lambda t: t[1]):
        for t, x0, x1 in spans:
            if (x1 - x0) > 2.5 * avg_w:
                continue
            cx = (x0 + x1) / 2
            j = next((j for j, (f0, f1) in enumerate(full) if f0 <= cx < f1), None)
            if j is not None:
                parts[j].append(t)
    # Unfilled header columns are left blank rather than "column_N" - a blank
    # <th> reads cleaner than a placeholder label in the output.
    names = [" ".join(parts[j]) if parts.get(j) else "" for j in range(k)]
    return names, sum(1 for j in range(k) if parts.get(j))


def _attach_wrapped_lines(lines, first_data_y, grid_y, cut_xs):
    """Merge short mid-table text lines (wrapped cell continuations) into the
    row above, in the aligned text column. Deterministic merge-up (the pre-LLM
    fallback): numeric spans and non-text columns are never attached."""
    full = _full_intervals(cut_xs)
    avg_w = _avg_col_width(cut_xs)
    k = len(cut_xs) + 1
    col_texty = []
    for j in range(k):
        vals = [cells[j] for cells, _ in grid_y if cells[j].strip()]
        texty = sum(1 for v in vals if _cell_class(v) == "text")
        col_texty.append(bool(vals) and texty >= 0.5 * len(vals))

    for spans, y0, _y1 in sorted(lines, key=lambda t: t[1]):
        if y0 < first_data_y or len(spans) >= MIN_CELLS_PER_ROW:
            continue
        above = [i for i, g in enumerate(grid_y) if g[1] <= y0]
        if not above:
            continue
        ti = above[-1]
        for t, x0, x1 in spans:
            if (x1 - x0) > 2.5 * avg_w:
                continue
            cx = (x0 + x1) / 2
            j = next((j for j, (f0, f1) in enumerate(full) if f0 <= cx < f1), None)
            if j is None or not col_texty[j] or _cell_class(t) != "text":
                continue
            target = grid_y[ti][0]
            target[j] = f"{target[j]} {t}".strip() if target[j] else t


_SYMBOL_ONLY = re.compile(r"^[^\w]{1,2}$")


def _merge_marker_columns(names, grid):
    """Merge symbol-marker columns into their right neighbor.

    A column whose every non-empty DATA value is a short non-alphanumeric
    symbol (checkboxes, bullets, tick marks) is a row *marker*, not a data
    column - keeping it separate splits "(checkbox) CODE" into two cells and
    shifts the header row out of alignment with the data columns. Runs after
    header extraction so the header row doesn't mask an all-symbol column."""
    if not grid:
        return names, grid
    k = len(grid[0])
    j = 0
    while k > 1 and j < k - 1:
        vals = [row[j] for row in grid if row[j].strip()]
        if len(vals) >= 2 and all(_SYMBOL_ONLY.match(v.strip()) for v in vals):
            for row in grid:
                row[j : j + 2] = [f"{row[j]} {row[j + 1]}".strip()]
            if names:
                names[j : j + 2] = [f"{names[j]} {names[j + 1]}".strip()]
            k -= 1
        else:
            j += 1
    return names, grid


def _build_html(names, grid, has_header: bool) -> str:
    out = ["<table>"]
    if has_header:
        out.append("<thead><tr>")
        out += [f"<th>{escape(n)}</th>" for n in names]
        out.append("</tr></thead>")
    out.append("<tbody>")
    for cells in grid:
        out.append("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def infer_table_column_cuts(lines, column_count):
    """Infer cuts from repeated content starts and the real inter-column gaps."""
    if column_count < 2:
        return None
    spans = [span for row, _y0, _y1 in lines for span in row]
    starts = sorted({round(x0, 1) for _text, x0, _x1 in spans})
    if len(starts) < column_count:
        return None
    same_line_pairs = set()
    for row, _y0, _y1 in lines:
        row_starts = sorted({round(x0, 1) for _text, x0, _x1 in row})
        same_line_pairs.update(itertools.pairwise(row_starts))
    candidates = []
    for split_at in itertools.combinations(range(len(starts) - 1), column_count - 1):
        boundaries = [(starts[i] + starts[i + 1]) / 2 for i in split_at]
        groups = [[] for _ in range(column_count)]
        for span in spans:
            groups[sum(span[1] >= boundary for boundary in boundaries)].append(span)
        crossed = sum((starts[i], starts[i + 1]) in same_line_pairs for i in split_at)
        score = (min(map(len, groups)), len(groups[0]), -crossed, sum(starts[i + 1] - starts[i] for i in split_at))
        candidates.append((score, split_at))
    if not candidates:
        return None
    _score, split_at = max(candidates)
    groups = []
    begin = 0
    for end in list(split_at) + [len(starts) - 1]:
        groups.append(starts[begin : end + 1])
        begin = end + 1
    start_boundaries = [(starts[i] + starts[i + 1]) / 2 for i in split_at]
    groups = [[] for _group in groups]
    for text, x0, x1 in spans:
        group = sum(x0 >= boundary for boundary in start_boundaries)
        groups[group].append((text, x0, x1))
    cuts = []
    for left, right in itertools.pairwise(groups):
        if not left or not right:
            return None
        left_edge = max(x1 for _text, _x0, x1 in left)
        right_edge = min(x0 for _text, x0, _x1 in right)
        if right_edge <= left_edge:
            return None
        cuts.append((left_edge + right_edge) / 2)
    return cuts


def reconstruct_continued_table_html(
    lines, names, cut_xs, pdftext_page=None, table_bbox=None
):
    """Reconstruct a headerless table continued from the preceding page."""
    lines = _coalesce_same_baseline(lines)
    k = len(cut_xs) + 1
    if len(names) != k or not lines:
        return None

    physical = []
    for spans, y0, y1 in lines:
        cells = [""] * k
        for text, x0, x1 in spans:
            center = (x0 + x1) / 2
            column = min(sum(center >= cut for cut in cut_xs), k - 1)
            cells[column] = f"{cells[column]} {text}".strip()
        physical.append([y0, y1, cells])
    line_height = median(max(1.0, y1 - y0) for y0, y1, _cells in physical)

    best_anchor = None
    for column in range(k):
        occupied = [(y0, y1) for y0, y1, cells in physical if cells[column]]
        if len(occupied) < 2:
            continue
        clusters = []
        for y0, y1 in occupied:
            if clusters and y0 - clusters[-1][-1][1] <= 0.4 * line_height:
                clusters[-1].append((y0, y1))
            else:
                clusters.append([(y0, y1)])
        if len(clusters) < 2:
            continue
        candidate = (len(occupied), column, clusters)
        if best_anchor is None or candidate[:2] < best_anchor[:2]:
            best_anchor = candidate
    if best_anchor is None:
        return None

    _occupied, _column, clusters = best_anchor
    anchors = [
        (cluster[0][0] + cluster[-1][1]) / 2 for cluster in clusters
    ]
    spacings = [right - left for left, right in itertools.pairwise(anchors)]
    if not spacings:
        return None
    first_boundary = anchors[0] - median(spacings) / 2
    boundaries = [
        (left + right) / 2 for left, right in itertools.pairwise(anchors)
    ]
    orphan = [""] * k
    grid = [[""] * k for _anchor in anchors]

    def add_cells(y0, y1, cells):
        center = (y0 + y1) / 2
        if center < first_boundary:
            target = orphan
        else:
            row = sum(center >= boundary for boundary in boundaries)
            target = grid[row]
        for column, text in enumerate(cells):
            if text:
                target[column] = f"{target[column]} {text}".strip()

    block_groups = []
    if pdftext_page and table_bbox:
        bx0, by0, bx1, by1 = table_bbox
        for pdf_block in pdftext_page.get("blocks", []):
            grouped_lines = []
            for line in pdf_block.get("lines", []):
                tokens = _line_tokens(line, table_bbox)
                if tokens:
                    lb = line.get("bbox") or [0, 0, 0, 0]
                    grouped_lines.append((tokens, lb[1], lb[3]))
            if grouped_lines:
                block_groups.append(grouped_lines)

    if block_groups:
        for grouped_lines in block_groups:
            mapped = []
            anchor_tokens = 0
            for spans, y0, y1 in grouped_lines:
                cells = [""] * k
                for text, x0, x1 in spans:
                    center = (x0 + x1) / 2
                    column = min(sum(center >= cut for cut in cut_xs), k - 1)
                    cells[column] = f"{cells[column]} {text}".strip()
                    if column == _column:
                        anchor_tokens += 1
                mapped.append((y0, y1, cells))
            if anchor_tokens > 1:
                for y0, y1, cells in mapped:
                    add_cells(y0, y1, cells)
            else:
                cells = [""] * k
                for _y0, _y1, line_cells in mapped:
                    for column, text in enumerate(line_cells):
                        if text:
                            cells[column] = f"{cells[column]} {text}".strip()
                add_cells(
                    min(y0 for y0, _y1, _cells in mapped),
                    max(y1 for _y0, y1, _cells in mapped),
                    cells,
                )
    else:
        for y0, y1, cells in physical:
            add_cells(y0, y1, cells)

    # A fragment above the first row-anchor which occupies no first column is
    # the tail of the preceding page's last row, not a prefix of this page's
    # first row.  Return it to the caller so it can be appended upstream.
    upstream_orphan = orphan if not orphan[0] and any(orphan[1:]) else None
    if any(orphan) and upstream_orphan is None:
        grid.insert(0, orphan)
    complete = sum(sum(bool(cell) for cell in row) >= 2 for row in grid)
    if complete < max(2, len(grid) - 1):
        return None
    return _build_html(names, grid, True), 0.95, upstream_orphan


def reconstruct_headerless_table_html(lines):
    """Recover a continuation table when its preceding page is not loaded."""
    lines = _coalesce_same_baseline(lines)
    if not lines:
        return None
    best = None
    for column_count in range(2, 7):
        cuts = infer_table_column_cuts(lines, column_count)
        if not cuts:
            continue
        physical = []
        for spans, y0, y1 in lines:
            cells = [""] * column_count
            for text, x0, x1 in spans:
                col = min(sum((x0 + x1) / 2 >= cut for cut in cuts), column_count - 1)
                cells[col] = f"{cells[col]} {text}".strip()
            physical.append((y0, y1, cells))
        line_height = min(max(1.0, y1 - y0) for y0, y1, _cells in physical)
        column_clusters = []
        for column in range(column_count):
            anchors = [(y0, y1) for y0, y1, cells in physical if cells[column]]
            clusters = []
            for anchor in anchors:
                if clusters and anchor[0] - clusters[-1][-1][1] <= line_height:
                    clusters[-1].append(anchor)
                else:
                    clusters.append([anchor])
            column_clusters.append(clusters)
        anchor_column, clusters = max(
            enumerate(column_clusters), key=lambda item: (len(item[1]), -item[0])
        )
        if len(clusters) < 2:
            continue
        if len(clusters) < 2:
            continue
        centers = [(c[0][0] + c[-1][1]) / 2 for c in clusters]
        boundaries = [(a + b) / 2 for a, b in itertools.pairwise(centers)]
        grid = [[""] * column_count for _ in centers]
        for y0, y1, cells in physical:
            row = sum((y0 + y1) / 2 >= boundary for boundary in boundaries)
            for col, text in enumerate(cells):
                if text:
                    grid[row][col] = f"{grid[row][col]} {text}".strip()
        filled = sum(sum(bool(cell) for cell in row) >= 2 for row in grid)
        if filled < max(2, len(grid) - 1):
            continue
        html = _build_html([""] * column_count, grid, False)
        rank = (column_count, filled, len(html))
        if best is None or rank > best[0]:
            best = (rank, html, 0.95)
    if best is None:
        return None
    rank, html, score = best
    empty_head = "<thead><tr>" + "<th></th>" * rank[0] + "</tr></thead>"
    return html.replace(empty_head, ""), score


def _wrapped_text_table(lines):
    """Reconstruct text-heavy tables whose cells wrap across physical lines.

    A sparse column (often an ID, level, price, or short label) provides the
    logical row anchors; all physical lines between consecutive anchors are
    then folded into the corresponding row.  This complements the regular
    span/projection grids, which require multiple cells on the same baseline.
    """
    ordered = sorted(lines, key=lambda item: item[1])
    header_index = next(
        (i for i, (spans, _y0, _y1) in enumerate(ordered) if 2 <= len(spans) <= 12),
        None,
    )
    if header_index is None:
        return None
    header_spans = ordered[header_index][0]
    centers = [(x0 + x1) / 2 for _text, x0, x1 in header_spans]
    if any(right - left < 20 for left, right in itertools.pairwise(centers)):
        return None
    cuts = [(left + right) / 2 for left, right in itertools.pairwise(centers)]
    k = len(header_spans)

    def column_for(x0, x1):
        center = (x0 + x1) / 2
        return min(sum(center >= cut for cut in cuts), k - 1)

    # Coalesce pdftext blocks that share a baseline (different table cells are
    # frequently emitted as independent lines with identical y coordinates).
    physical = []  # [y0, y1, cells]
    for spans, y0, y1 in ordered[header_index + 1 :]:
        cells = [""] * k
        for text, x0, x1 in spans:
            column = column_for(x0, x1)
            cells[column] = f"{cells[column]} {text}".strip()
        if physical and abs(physical[-1][0] - y0) <= 1.5:
            physical[-1][1] = max(physical[-1][1], y1)
            for column, text in enumerate(cells):
                if text:
                    physical[-1][2][column] = (
                        f"{physical[-1][2][column]} {text}".strip()
                    )
        else:
            physical.append([y0, y1, cells])
    if len(physical) < MIN_TABLE_ROWS:
        return None

    line_height = median(max(1.0, y1 - y0) for y0, y1, _cells in physical)
    best_anchor = None
    for column in range(k):
        occupied = [(y0, y1) for y0, y1, cells in physical if cells[column]]
        if len(occupied) < 2:
            continue
        clusters = []
        for y0, y1 in occupied:
            if clusters and y0 - clusters[-1][-1][1] <= 1.75 * line_height:
                clusters[-1].append((y0, y1))
            else:
                clusters.append([(y0, y1)])
        if len(clusters) < 2:
            continue
        candidate = (len(occupied), column, clusters)
        if best_anchor is None or candidate[:2] < best_anchor[:2]:
            best_anchor = candidate
    if best_anchor is None:
        return None

    _occupied_count, _anchor_column, clusters = best_anchor
    boundaries = [
        (clusters[index][-1][1] + clusters[index + 1][0][0]) / 2
        for index in range(len(clusters) - 1)
    ]
    grid = [[""] * k for _cluster in clusters]
    for y0, y1, cells in physical:
        row = sum((y0 + y1) / 2 >= boundary for boundary in boundaries)
        for column, text in enumerate(cells):
            if text:
                grid[row][column] = f"{grid[row][column]} {text}".strip()

    # Reject weak guesses: most logical rows must contain at least two cells.
    filled_rows = sum(sum(bool(cell) for cell in row) >= 2 for row in grid)
    if filled_rows / len(grid) < 0.7:
        return None
    names = [text for text, _x0, _x1 in header_spans]
    return _build_html(names, grid, True), 0.9


def _line_tokens(line: dict, bbox):
    """Tokenize a pdftext line into (text, x0, x1) cells inside ``bbox``.

    Prefer WORD-level tokens split on intra-line character gaps: pdftext often
    merges adjacent table cells into one span, which collapses the column
    structure the grid heuristics rely on. Re-splitting the chars at gaps
    wider than ~a quarter of the line height recovers per-cell tokens. Falls
    back to raw spans when char data isn't kept."""
    bx0, by0, bx1, by1 = bbox

    def inside(x0, y0, x1, y1):
        return bx0 <= (x0 + x1) / 2 <= bx1 and by0 <= (y0 + y1) / 2 <= by1

    tokens = []
    have_chars = False
    for s in line.get("spans", []):
        chars = s.get("chars") or []
        if not chars:
            continue
        have_chars = True
        cur = None  # [text, x0, x1]
        for c in chars:
            cx0, cy0, cx1, cy1 = c["bbox"]
            if not inside(cx0, cy0, cx1, cy1):
                continue
            ch = c.get("char", "")
            # A full character height keeps ordinary justified-word gaps in a
            # single cell while still splitting the much wider column gaps.
            gap = 1.0 * max(cy1 - cy0, 1.0)
            if cur is None:
                cur = [ch, cx0, cx1]
            elif cx0 - cur[2] > gap:
                tokens.append(cur)
                cur = [ch, cx0, cx1]
            else:
                cur[0] += ch
                cur[2] = cx1
        if cur is not None:
            tokens.append(cur)

    if not have_chars:
        # No char data: fall back to span-level tokens.
        for s in line.get("spans", []):
            t = (s.get("text") or "").strip()
            sx0, sy0, sx1, sy1 = s["bbox"]
            if t and inside(sx0, sy0, sx1, sy1):
                tokens.append([t, sx0, sx1])

    # pdftext may split adjacent words (or a currency symbol and its value)
    # into separate spans.  Join only neighbors whose horizontal gap is no
    # larger than the line height; true table-column gaps are much wider.
    line_bbox = line.get("bbox") or [0, 0, 0, 0]
    join_gap = max(float(line_bbox[3] - line_bbox[1]), 1.0)
    joined = []
    for text, x0, x1 in sorted(tokens, key=lambda token: token[1]):
        if joined and x0 - joined[-1][2] <= join_gap:
            separator = (
                ""
                if re.search(r"\d$", joined[-1][0]) and re.match(r"\d", text)
                else " "
            )
            joined[-1][0] = f"{joined[-1][0]}{separator}{text}".strip()
            joined[-1][2] = max(joined[-1][2], x1)
        else:
            joined.append([text, x0, x1])
    tokens = joined

    out = []
    for text, x0, x1 in tokens:
        text = text.strip()
        if text and not _LEADER_ONLY.match(text):
            out.append((text, round(x0, 1), round(x1, 1)))
    return out


def table_lines_from_pdftext(pdftext_page: dict, bbox) -> list:
    """Extract ``[(tokens, y0, y1)]`` lines (tokens = ``[(text, x0, x1)]``) from a
    cached pdftext page, restricted to ``bbox`` (x0, y0, x1, y1, in pdftext/PDF
    points). Tokens are word-level (see _line_tokens). Feeds
    reconstruct_table_html."""
    bx0, by0, bx1, by1 = bbox
    lines = []
    for block in pdftext_page.get("blocks", []):
        for line in block.get("lines", []):
            lb = line.get("bbox")
            # Cheap reject: _line_tokens only keeps chars whose center lies in
            # the table bbox, and every char is contained in the line bbox, so a
            # line whose bbox is disjoint from the table region can contribute
            # nothing. Skipping it avoids re-scanning the whole page's char
            # layer once per table (was O(tables x page_chars)). Lines without a
            # bbox fall through to the full per-char check.
            if lb is not None and (
                lb[2] < bx0 or lb[0] > bx1 or lb[3] < by0 or lb[1] > by1
            ):
                continue
            toks = _line_tokens(line, bbox)
            if toks:
                lb = lb or [0, 0, 0, 0]
                lines.append((toks, round(lb[1], 1), round(lb[3], 1)))
    return lines


def reconstruct_table_html(lines):
    """Reconstruct ``(html, score)`` from a table's pdftext lines, or None.

    ``lines``: [(spans, y0, y1)], spans = [(text, x0, x1)] in PDF points.
    """
    if not lines:
        return None
    lines = _coalesce_same_baseline(lines)
    # Layout blocks can include the preceding paragraph/list before a real
    # table.  A wide, multi-column header is a geometry-based table start;
    # trim only when there are later rows to support it.
    header_start = None
    for index, (spans, _y0, _y1) in enumerate(lines):
        starts = [x0 for _text, x0, _x1 in spans]
        if len(starts) >= 3 and max(b - a for a, b in itertools.pairwise(sorted(starts))) >= 40:
            if any(len(row) >= 2 for row, _a, _b in lines[index + 1 :]):
                header_start = index
                break
    if header_start is not None and header_start:
        lines = lines[header_start:]
    if not _garble_ok(lines):
        return None

    first_data_y = _find_header_band(lines)
    if first_data_y is None:
        wrapped = _wrapped_text_table(lines)
        if wrapped:
            return wrapped
        # No numeric transition to mark the header. Fall back to the first
        # span-rich data row: sparse lines above it are a (possibly multi-line)
        # header band, so their text is captured instead of dropped.
        rich_ys = [y0 for spans, y0, _ in lines if len(spans) >= MIN_CELLS_PER_ROW]
        if rich_ys:
            alt_y = min(rich_ys)
            if any(y0 < alt_y for _, y0, _ in lines):
                first_data_y = alt_y
    if first_data_y is not None:
        data = [
            (spans, y0)
            for spans, y0, _ in lines
            if y0 >= first_data_y and len(spans) >= MIN_CELLS_PER_ROW
        ]
        band = [(spans, y0) for spans, y0, _ in lines if y0 < first_data_y]
        n_hdr = (
            len([1 for spans, _ in band for t, x0, x1 in spans if (x1 - x0) < 200])
            or None
        )
    else:
        data = [
            (spans, y0) for spans, y0, _ in lines if len(spans) >= MIN_CELLS_PER_ROW
        ]
        band, n_hdr = [], None

    data_rows = [spans for spans, _ in data]
    cands = _candidates(data_rows)
    if not cands:
        return None
    _name, best, score = _pick_winner(cands, n_hdr)
    if not best:
        return None
    k, cut_xs, grid, counts = best

    if band:
        names, n_named = _stitch_band_headers(band, cut_xs, k)
        has_header = True
    else:
        # No geometric header band: treat the first data row as the header
        # (the common header-in-first-row case); avoids emitting column_N junk.
        names, has_header = [], False

    if len(grid) < MIN_TABLE_ROWS:
        return None

    grid_row_ys = []
    if data:
        grid_y = list(zip(grid, [y0 for _, y0 in data[-len(grid) :]]))
        attachment_start_y = first_data_y if first_data_y is not None else data[0][1]
        _attach_wrapped_lines(lines, attachment_start_y, grid_y, cut_xs)
        grid_row_ys = [y0 for _cells, y0 in grid_y]
        grid = [g for g, _ in grid_y]

    if not has_header and grid:
        names, grid, has_header = grid[0], grid[1:], True
        if grid_row_ys:
            header_y, grid_row_ys = grid_row_ys[0], grid_row_ys[1:]
            # Fold incomplete rows into a multiline header when their baseline
            # is closer to the header than to the following, fuller data row.
            while len(grid) >= 2 and len(grid_row_ys) >= 2:
                candidate_fill = sum(bool(cell.strip()) for cell in grid[0])
                next_fill = sum(bool(cell.strip()) for cell in grid[1])
                header_gap = grid_row_ys[0] - header_y
                next_gap = grid_row_ys[1] - grid_row_ys[0]
                if not (
                    0 < candidate_fill < next_fill
                    and header_gap > 0
                    and next_gap > 0
                    and header_gap <= 0.8 * next_gap
                ):
                    break
                for column, text in enumerate(grid[0]):
                    if text:
                        names[column] = f"{names[column]} {text}".strip()
                header_y = grid_row_ys[0]
                grid = grid[1:]
                grid_row_ys = grid_row_ys[1:]
        if len(grid) < 1:
            return None

    names, grid = _merge_marker_columns(names, grid)

    return _build_html(names, grid, has_header), score
