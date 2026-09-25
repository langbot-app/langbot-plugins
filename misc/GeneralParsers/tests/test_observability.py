from __future__ import annotations

import json
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _ensure_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = _ensure_module(parent_name)
        setattr(parent, child_name, module)
    return module


def _install_page_stubs() -> None:
    page_module = _ensure_module("langbot_plugin.api.definition.components.page")

    @dataclass
    class PageRequest:
        endpoint: str
        method: str
        body: object = None
        caller: dict | None = None
        headers: dict | None = None

    @dataclass
    class PageResponse:
        data: object = None
        error: str | None = None

        @classmethod
        def ok(cls, data=None):
            return cls(data=data)

        @classmethod
        def fail(cls, error: str):
            return cls(error=error)

    class Page:
        __kind__ = "Page"

        async def initialize(self) -> None:
            pass

    page_module.Page = Page
    page_module.PageRequest = PageRequest
    page_module.PageResponse = PageResponse


_install_page_stubs()


class ParserTelemetryTests(unittest.TestCase):
    def test_snapshot_records_only_operational_metadata(self) -> None:
        from components.observability.telemetry import ParserTelemetry

        telemetry = ParserTelemetry(recent_limit=2)
        telemetry.record_parse(
            filename="/tmp/report.pdf",
            mime_type="application/pdf",
            extension="pdf",
            extension_source="mime_type",
            duration_ms=12.345,
            text_chars=1200,
            sections_count=4,
            metadata={
                "has_tables": True,
                "has_images": True,
                "images_count": 2,
                "vision_used": True,
                "vision_tasks_count": 3,
                "vision_images_described_count": 2,
                "vision_failed_count": 1,
            },
        )

        snapshot = telemetry.snapshot()

        self.assertEqual(snapshot["summary"]["total_parses"], 1)
        self.assertEqual(snapshot["summary"]["failed_parses"], 0)
        self.assertEqual(snapshot["summary"]["tables_detected"], 1)
        self.assertEqual(snapshot["summary"]["images_detected"], 1)
        self.assertEqual(snapshot["summary"]["vision_tasks_count"], 3)
        self.assertEqual(snapshot["distributions"]["extensions"][0]["key"], "pdf")
        self.assertEqual(snapshot["recent_parses"][0]["filename"], "report.pdf")
        self.assertNotIn("text", snapshot["recent_parses"][0])
        self.assertNotIn("file_content", snapshot["recent_parses"][0])

    def test_ring_buffers_and_errors_are_bounded(self) -> None:
        from components.observability.telemetry import ParserTelemetry

        telemetry = ParserTelemetry(recent_limit=1)
        telemetry.record_parse(
            filename="ok.txt",
            mime_type="text/plain",
            extension="txt",
            extension_source="mime_type",
            duration_ms=1,
            text_chars=2,
            sections_count=1,
        )
        telemetry.record_parse(
            filename="bad.pdf",
            mime_type="application/pdf",
            extension="pdf",
            extension_source="mime_type",
            duration_ms=2,
            text_chars=0,
            sections_count=0,
            metadata={"parser_failed": True, "parse_error": "broken"},
        )

        snapshot = telemetry.snapshot()

        self.assertEqual(snapshot["summary"]["total_parses"], 2)
        self.assertEqual(snapshot["summary"]["failed_parses"], 1)
        self.assertEqual(len(snapshot["recent_parses"]), 1)
        self.assertEqual(snapshot["recent_parses"][0]["filename"], "bad.pdf")
        self.assertEqual(len(snapshot["recent_errors"]), 1)
        self.assertEqual(snapshot["recent_errors"][0]["parse_error"], "broken")


class ParserObservabilityPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from components.observability import get_telemetry

        get_telemetry().clear()

    async def test_snapshot_endpoint_returns_telemetry(self) -> None:
        from components.observability import get_telemetry
        from components.pages.observability import ParserObservabilityPage
        from langbot_plugin.api.definition.components.page import PageRequest

        get_telemetry().record_parse(
            filename="notes.md",
            mime_type="text/markdown",
            extension="md",
            extension_source="mime_type",
            duration_ms=4,
            text_chars=80,
            sections_count=2,
        )

        response = await ParserObservabilityPage().handle_api(
            PageRequest(endpoint="/snapshot", method="GET")
        )

        self.assertIsNone(response.error)
        self.assertEqual(response.data["summary"]["total_parses"], 1)
        self.assertEqual(response.data["recent_parses"][0]["extension"], "md")

    async def test_clear_endpoint_resets_telemetry(self) -> None:
        from components.observability import get_telemetry
        from components.pages.observability import ParserObservabilityPage
        from langbot_plugin.api.definition.components.page import PageRequest

        get_telemetry().record_parse(
            filename="broken.pdf",
            mime_type="application/pdf",
            extension="pdf",
            extension_source="mime_type",
            duration_ms=4,
            text_chars=0,
            sections_count=0,
            metadata={"parser_failed": True, "parse_error": "broken"},
        )

        response = await ParserObservabilityPage().handle_api(
            PageRequest(endpoint="/clear", method="POST")
        )

        self.assertIsNone(response.error)
        self.assertEqual(response.data["summary"]["total_parses"], 0)
        self.assertEqual(response.data["recent_errors"], [])

    async def test_unknown_endpoint_fails(self) -> None:
        from components.pages.observability import ParserObservabilityPage
        from langbot_plugin.api.definition.components.page import PageRequest

        response = await ParserObservabilityPage().handle_api(
            PageRequest(endpoint="/missing", method="GET")
        )

        self.assertIsNotNone(response.error)

    def test_i18n_assets_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for filename in ("en_US.json", "zh_Hans.json"):
            path = root / "components" / "pages" / "i18n" / filename
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("title", data)
            self.assertIn("sections.recentParses", data)


if __name__ == "__main__":
    unittest.main()
