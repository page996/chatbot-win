from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDEBAR_DIR = ROOT / "app" / "personal_wechat_bot" / "ui" / "sidebar"


class SidebarUiContractTest(unittest.TestCase):
    def test_javascript_referenced_ids_exist_in_sidebar_html(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        selector_ids = set(re.findall(r'["\']#([A-Za-z0-9_-]+)', js))

        missing = sorted(selector_ids - html_ids)

        self.assertEqual(missing, [])

    def test_navigation_buttons_have_matching_panels_and_statuses(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        pages = re.findall(r'<button[^>]+data-page="([^"]+)"', html)
        panels = re.findall(r'<button[^>]+data-panel="([^"]+)"', html)
        statuses = re.findall(r'<button[^>]+data-status="([^"]+)"', html)

        self.assertEqual(sorted(f"{page}Page" for page in pages if f"{page}Page" not in html_ids), [])
        self.assertEqual(sorted(f"{panel}Panel" for panel in panels if f"{panel}Panel" not in html_ids), [])
        self.assertEqual(
            statuses,
            ["pending", "approved", "queued_to_bridge", "accepted", "rejected", "sent", "failed"],
        )

    def test_id_buttons_are_bound_or_are_form_submit_buttons(self) -> None:
        html = (SIDEBAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        button_ids = set(re.findall(r'<button[^>]+id="([^"]+)"', html))
        selector_ids = set(re.findall(r'["\']#([A-Za-z0-9_-]+)', js))
        submit_buttons = {
            match.group(1)
            for match in re.finditer(r'<button[^>]+id="([^"]+)"[^>]+type="submit"', html)
        }

        unbound = sorted(button_ids - selector_ids - submit_buttons)

        self.assertEqual(unbound, [])

    def test_storage_status_prioritizes_database_contracts_before_component_compaction(self) -> None:
        js = (SIDEBAR_DIR / "app.js").read_text(encoding="utf-8")
        helper = re.search(
            r"function storageStatusDisplayPayload\(payload\) \{(?P<body>.*?)\n\}",
            js,
            flags=re.DOTALL,
        )

        self.assertIsNotNone(helper)
        body = helper.group("body")
        self.assertIn("database_contract_summary: payload?.database_contract_summary", body)
        self.assertIn("database_contracts: Array.isArray(payload?.database_contracts)", body)
        self.assertIn("components: compactPayload", body)
        self.assertNotIn("database_contracts: compactPayload", body)


if __name__ == "__main__":
    unittest.main()
