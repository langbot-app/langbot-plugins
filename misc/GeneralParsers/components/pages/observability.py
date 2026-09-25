from __future__ import annotations

from langbot_plugin.api.definition.components.page import Page, PageRequest, PageResponse

from ..observability import get_telemetry


class ParserObservabilityPage(Page):
    """Backend endpoints for the static parser observability page."""

    async def handle_api(self, request: PageRequest) -> PageResponse:
        endpoint = (request.endpoint or "").rstrip("/") or "/"
        method = (request.method or "POST").upper()

        if endpoint == "/snapshot" and method in {"GET", "POST"}:
            return PageResponse.ok(get_telemetry().snapshot())

        if endpoint == "/clear" and method in {"POST", "DELETE"}:
            get_telemetry().clear()
            return PageResponse.ok(get_telemetry().snapshot())

        return PageResponse.fail(f"Unsupported endpoint: {method} {endpoint}")
