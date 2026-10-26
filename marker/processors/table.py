from collections import Counter
from typing import Annotated, List

from bs4 import BeautifulSoup
from surya.layout.schema import LayoutBox, LayoutResult
from surya.recognition import RecognitionPredictor, _detect_repeat_loop

from marker.processors import BaseProcessor
from marker.processors.table_recon import (
    _build_html,
    infer_table_column_cuts,
    reconstruct_table_html,
    reconstruct_continued_table_html,
    reconstruct_headerless_table_html,
    table_lines_from_pdftext,
)
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.labels import block_type_to_surya_label
from marker.util import matrix_intersection_area
from marker.logger import get_logger

logger = get_logger()


def reconstruct_wide_header_table(lines):
    """Deterministic reconstruction for a wide multi-column header block."""
    ordered = sorted(lines, key=lambda item: item[1])
    header = next((item for item in ordered if len(item[0]) >= 3 and
                   max(b - a for a, b in zip(sorted(x[1] for x in item[0]),
                                              sorted(x[1] for x in item[0])[1:])) >= 40), None)
    if header is None:
        return None
    spans, hy0, _hy1 = header
    names = [text for text, _x0, _x1 in spans]
    centers = [(x0 + x1) / 2 for _text, x0, x1 in spans]
    cuts = [(a + b) / 2 for a, b in zip(centers, centers[1:])]
    physical = []
    for row, y0, y1 in ordered:
        if y0 <= _hy1:
            continue
        cells = [""] * len(names)
        for text, x0, x1 in row:
            col = min(sum((x0 + x1) / 2 >= cut for cut in cuts), len(names) - 1)
            cells[col] = f"{cells[col]} {text}".strip()
        physical.append((y0, y1, cells))
    if not physical:
        return None
    anchor_col = len(names) - 1
    anchors = [(y0 + y1) / 2 for y0, y1, cells in physical if cells[anchor_col]]
    if len(anchors) < 2:
        return None
    boundaries = [(a + b) / 2 for a, b in zip(anchors, anchors[1:])]
    grid = [[""] * len(names) for _ in anchors]
    for y0, y1, cells in physical:
        row_index = sum((y0 + y1) / 2 >= boundary for boundary in boundaries)
        for col, text in enumerate(cells):
            if text:
                grid[row_index][col] = f"{grid[row_index][col]} {text}".strip()
    if sum(sum(bool(cell) for cell in row) >= 2 for row in grid) < 2:
        return None
    return _build_html(names, grid, True)


def append_upstream_fragment(block, cells):
    """Append a headerless continuation fragment to the previous table row."""
    if block is None or not block.html:
        return
    soup = BeautifulSoup(block.html, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        return
    previous_cells = rows[-1].find_all("td")
    for index, text in enumerate(cells):
        if text and index < len(previous_cells):
            old = previous_cells[index].get_text(" ", strip=True)
            previous_cells[index].string = f"{old} {text}".strip()
    block.html = str(soup)


def detach_leading_continuation(lines):
    """Detach text above the first left-column row of a continued table."""
    if not lines:
        return lines, ""
    left_x = min(x0 for spans, _y0, _y1 in lines for _text, x0, _x1 in spans)
    first_left_y = min(
        y0 for spans, y0, _y1 in lines
        if any(x0 <= left_x + 2 for _text, x0, _x1 in spans)
    )
    leading = [(spans, y0, y1) for spans, y0, y1 in lines if y1 <= first_left_y]
    if not leading:
        return lines, ""
    # Only detach a fragment that has no left-column cell; this prevents
    # ordinary first rows or headers from being moved to the prior page.
    if any(any(x0 <= left_x + 2 for _text, x0, _x1 in spans) for spans, _y0, _y1 in leading):
        return lines, ""
    text = " ".join(text for spans, _y0, _y1 in leading for text, _x0, _x1 in spans)
    return [line for line in lines if line not in leading], text


def append_fragment_to_longest_cell(block, text):
    if not text or block is None or not block.html:
        return
    soup = BeautifulSoup(block.html, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        return
    cells = rows[-1].find_all("td")
    if not cells:
        return
    cell = max(cells, key=lambda item: len(item.get_text(" ", strip=True)))
    cell.string = f"{cell.get_text(' ', strip=True)} {text}".strip()
    block.html = str(soup)


class TableProcessor(BaseProcessor):
    """Fills in table HTML.

    - Scanned/garbled pages are full-page OCR'd by the OcrBuilder, which already
      sets ``block.html`` on their Table blocks - those are left untouched.
    - Digital (pdftext) tables are reconstructed from the PDF text layer with
      CPU-only heuristics (see ``table_recon``), which emit the table HTML.
    - A digital table the heuristics can't resolve (sparse, vector-drawn, low
      score) falls back to OCRing the table crop with the recognition model.

    There is no dedicated table-structure model.
    """

    block_types = (BlockTypes.Table, BlockTypes.TableOfContents, BlockTypes.Form)
    mode: Annotated[str, "Conversion mode ('balanced' | 'fast')."] = "balanced"
    min_recon_score: Annotated[
        float,
        "Minimum pdftext reconstruction judge score to accept; below this a",
        "digital table falls back to recognition OCR. None = auto by mode:",
        "0.75 in balanced (spend VLM calls on low-confidence tables), 0.5 in",
        "fast (keep VLM fallback rare).",
    ] = None
    ocr_table_token_floor: Annotated[
        int,
        "Minimum token budget when OCRing a table crop to HTML.",
    ] = 2048
    contained_block_types: Annotated[
        List[BlockTypes],
        "Block types to remove if they're contained inside the tables.",
    ] = (BlockTypes.Text, BlockTypes.TextInlineMath)
    disable_tqdm: Annotated[
        bool,
        "Whether to disable the tqdm progress bar.",
    ] = False
    disable_ocr: Annotated[bool, "Disable OCR entirely."] = False

    def __init__(
        self,
        recognition_model: RecognitionPredictor,
        config=None,
    ):
        super().__init__(config)

        self.recognition_model = recognition_model
        # Conversion stats, useful for monitoring table quality
        self.table_stats = Counter()

    def __call__(self, document: Document):
        tables_by_page = self.collect_tables(document)
        total = sum(len(v) for v in tables_by_page.values())
        if not total:
            return

        ocr_fallback = []  # (page, block) digital tables the heuristics missed
        continuation = None
        previous_tail = None
        for page in document.pages:
            page_tables = sorted(
                tables_by_page.get(page.page_id, []), key=lambda block: block.polygon.bbox[1]
            )
            schemas = {}
            for block in page_tables:
                # Scanned/garbled pages: the full-page OCR already produced the
                # table HTML - trust it, don't redo.
                if block.html:
                    self.table_stats["tables_ocr"] += 1
                    continue

                lines = table_lines_from_pdftext(
                    page.pdftext_page or {}, block.polygon.bbox
                )
                if previous_tail is not None:
                    _width, height = page.polygon.size
                    bx0, by0, bx1, _by1 = block.polygon.bbox
                    px0, _py0, px1, _py1 = previous_tail.polygon.bbox
                    overlap = max(0.0, min(bx1, px1) - max(bx0, px0))
                    aligned = overlap / max(min(bx1 - bx0, px1 - px0), 1) >= 0.8
                    if by0 <= 0.15 * height and aligned:
                        lines, fragment = detach_leading_continuation(lines)
                        append_fragment_to_longest_cell(previous_tail, fragment)
                html = None
                inherited_schema = None
                if continuation is not None:
                    page_width, page_height = page.polygon.size
                    bx0, by0, bx1, _by1 = block.polygon.bbox
                    px0, _py0, px1, _py1 = continuation["bbox"]
                    overlap = max(0.0, min(bx1, px1) - max(bx0, px0))
                    aligned = overlap / max(min(bx1 - bx0, px1 - px0), 1) >= 0.8
                    if (
                        page.page_id == continuation["page_id"] + 1
                        and by0 <= 0.15 * page_height
                        and aligned
                    ):
                        result = reconstruct_continued_table_html(
                            lines,
                            continuation["names"],
                            continuation["cuts"],
                            page.pdftext_page,
                            block.polygon.bbox,
                        )
                        if result:
                            html, _score, upstream_orphan = result
                            inherited_schema = continuation
                            if upstream_orphan is not None:
                                append_upstream_fragment(
                                    continuation.get("block"), upstream_orphan
                                )
                if html is None:
                    has_wide_header = any(
                        len(spans) >= 3 and max(
                            b - a for a, b in zip(
                                sorted(x0 for _text, x0, _x1 in spans),
                                sorted(x0 for _text, x0, _x1 in spans)[1:],
                            )
                        ) >= 40
                        for spans, _y0, _y1 in lines
                    )
                    if not has_wide_header:
                        standalone = reconstruct_headerless_table_html(lines)
                        if standalone:
                            html, _score = standalone
                if html is None:
                    direct = reconstruct_table_html(lines)
                    html = direct[0] if direct else self.reconstruct_digital_table(page, block)
                if html:
                    block.structure = []
                    block.html = html
                    block.text_extraction_method = "pdftext"
                    self.table_stats["tables_pdftext"] += 1
                    if inherited_schema is not None:
                        schemas[block.id] = {**inherited_schema, "block": block}
                    else:
                        soup = BeautifulSoup(html, "html.parser")
                        header_cells = soup.find_all("th")
                        names = [cell.get_text(" ", strip=True) for cell in header_cells]
                        cuts = infer_table_column_cuts(lines, len(names))
                        if names and cuts:
                            schemas[block.id] = {
                                "names": names,
                                "cuts": cuts,
                                "bbox": block.polygon.bbox,
                                "page_id": page.page_id,
                                "block": block,
                            }
                else:
                    ocr_fallback.append((page, block))

            continuation = None
            previous_tail = None
            if page_tables:
                last_block = max(page_tables, key=lambda block: block.polygon.bbox[3])
                schema = schemas.get(last_block.id)
                _page_width, page_height = page.polygon.size
                if schema and last_block.polygon.bbox[3] >= 0.85 * page_height:
                    continuation = {
                        **schema,
                        "bbox": last_block.polygon.bbox,
                        "page_id": page.page_id,
                    }
                if last_block.polygon.bbox[3] >= 0.85 * page_height:
                    previous_tail = last_block

        self.run_ocr_fallback(document, ocr_fallback)
        self.cleanup_contained_blocks(document, tables_by_page)

        # Release the cached raw pdftext pages - they hold char-level data
        for page in document.pages:
            page.pdftext_page = None

        self.table_stats["tables_total"] = total
        logger.info(f"Table processing stats: {dict(self.table_stats)}")

    def collect_tables(self, document: Document) -> dict:
        return {
            page.page_id: page.contained_blocks(document, self.block_types)
            for page in document.pages
        }

    def reconstruct_digital_table(self, page, block) -> str | None:
        """Reconstruct a digital table's HTML from the cached pdftext page.
        Returns HTML, or None if there's no text layer or the heuristics can't
        resolve a confident grid."""
        pdftext_page = page.pdftext_page
        if not pdftext_page:
            return None
        lines = table_lines_from_pdftext(pdftext_page, block.polygon.bbox)
        result = reconstruct_table_html(lines)
        if not result:
            return None
        html, score = result
        min_score = self.min_recon_score
        if min_score is None:
            min_score = 0.75 if self.mode == "balanced" else 0.5
        if score < min_score:
            return None
        return html

    def run_ocr_fallback(self, document: Document, fallback: list):
        """OCR the crops of digital tables the pdftext heuristics couldn't
        resolve, with the recognition model (one box per table -> HTML)."""
        if not fallback or self.disable_ocr:
            return

        images, entries = [], []
        for page, block in fallback:
            highres = page.get_image(highres=True)
            image_poly = block.polygon.rescale(page.polygon.size, highres.size)
            images.append(highres.crop(image_poly.bbox))
            entries.append((block, block.layout_token_count or 0))

        layout_results = []
        for image, (block, token_count) in zip(images, entries):
            w, h = image.size
            label = block_type_to_surya_label(block.block_type) or "Table"
            box = LayoutBox(
                polygon=[[0, 0], [w, 0], [w, h], [0, h]],
                label=label,
                raw_label=label,
                position=0,
                count=max(token_count, self.ocr_table_token_floor),
            )
            layout_results.append(LayoutResult(bboxes=[box], image_bbox=[0, 0, w, h]))

        self.recognition_model.disable_tqdm = self.disable_tqdm
        results = self.recognition_model(
            images=images, layout_results=layout_results, full_page=False
        )
        for (block, _tc), page_result in zip(entries, results):
            block_result = page_result.blocks[0] if page_result.blocks else None
            raw = block_result.html if block_result and not block_result.error else ""
            if block.block_type == BlockTypes.Form:
                # Forms rarely OCR to <table> html - accept any cleaned output
                # (matches how full-page OCR sets form html unconditionally).
                html = self.clean_form_html(raw)
            else:
                html = self.clean_table_html(raw)
            if not html:
                self.table_stats["tables_ocr_failed"] += 1
                logger.warning(f"Table OCR failed for block {block.id}")
                continue
            block.structure = []
            block.html = html
            block.text_extraction_method = "surya"
            self.table_stats["tables_ocr"] += 1

    def clean_table_html(self, html: str | None) -> str:
        if not html:
            return ""
        if "<table" not in html:
            return ""
        if _detect_repeat_loop(html):
            return ""

        # Re-serialize to balance tags in case of token-budget truncation
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None or not table.find_all(["td", "th"]):
            return ""
        return str(soup).strip()

    def clean_form_html(self, html: str | None) -> str:
        if not html or _detect_repeat_loop(html):
            return ""
        # Balance any truncated tags; no <table> requirement for forms.
        return str(BeautifulSoup(html, "html.parser")).strip()

    def cleanup_contained_blocks(self, document: Document, tables_by_page: dict):
        # Clean out other blocks inside the table
        # This can happen with stray text blocks inside the table post-merging
        for page in document.pages:
            page_tables = tables_by_page.get(page.page_id, [])
            if not page_tables:
                continue
            child_contained_blocks = page.contained_blocks(
                document, self.contained_block_types
            )
            if not child_contained_blocks:
                continue

            intersections = matrix_intersection_area(
                [c.polygon.bbox for c in child_contained_blocks],
                [block.polygon.bbox for block in page_tables],
            )
            for child_idx, child in enumerate(child_contained_blocks):
                for table_idx in range(len(page_tables)):
                    intersection_pct = intersections[child_idx, table_idx] / max(
                        child.polygon.area, 1
                    )
                    if intersection_pct > 0.95 and child.id in page.structure:
                        page.structure.remove(child.id)
                        break
