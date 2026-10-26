"""Optional regression tests for the user's locally held CCNL sample PDF.

Set MARKER_CCNL_PDF to an OCRmyPDF-processed copy of the sample before running.
The PDF itself is intentionally not committed to the repository.
"""

import os
import unittest

from bs4 import BeautifulSoup

from marker.processors.table_recon import (
    reconstruct_headerless_table_html,
    table_lines_from_pdftext,
)
from marker.providers.pdf import PdfProvider


@unittest.skipUnless(os.environ.get("MARKER_CCNL_PDF"), "MARKER_CCNL_PDF not set")
class HeaderlessCCNLRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = PdfProvider(
            os.environ["MARKER_CCNL_PDF"], {"page_range": [81, 82]}
        )

    def rows(self, page_number, bbox):
        lines = table_lines_from_pdftext(
            self.provider.raw_pdftext_pages[page_number], bbox
        )
        result = reconstruct_headerless_table_html(lines)
        self.assertIsNotNone(result)
        table = BeautifulSoup(result[0], "html.parser")
        return [
            [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            for row in table.find_all("tr")
        ]

    def test_adjacent_rows_remain_separate(self):
        rows = self.rows(81, (50, 40, 560, 780))
        by_title = {cells[0]: cells for cells in rows}
        self.assertIn("ICT Trainer", by_title)
        self.assertIn("Network Specialist", by_title)
        self.assertEqual(by_title["ICT Trainer"][-1], "4")
        self.assertEqual(by_title["Network Specialist"][-1], "4")
        self.assertTrue(by_title["ICT Security Specialist"][1].endswith("colleghi"))

    def test_wrapped_labels_and_descriptions_stay_together(self):
        rows = self.rows(82, (50, 60, 560, 220))
        by_title = {cells[0]: cells for cells in rows}
        self.assertIn("Service Desk Agent", by_title)
        self.assertIn("System Administrator", by_title)
        self.assertIn("Systems Analyst", by_title)
        self.assertIn("Technical Specialist", by_title)
        self.assertTrue(by_title["Technical Specialist"][1].startswith("Mantiene"))
        self.assertNotIn("Mantiene", by_title["Systems Analyst"][1])


if __name__ == "__main__":
    unittest.main()
