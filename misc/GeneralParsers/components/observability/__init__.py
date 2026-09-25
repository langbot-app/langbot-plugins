"""Import-safe observability helpers for parser components."""

from .telemetry import ParserTelemetry, get_telemetry

__all__ = ["ParserTelemetry", "get_telemetry"]
