"""Outbound-safety regression tests for the WebSearch ``visit_web`` tool.

These cover the two blocking certification findings:

* F1 - the user-supplied URL is an unvalidated server-side request target. The
  tests prove that loopback / link-local / private / metadata ranges, special
  addresses and non-HTTP(S) schemes are refused before a connection is made,
  that every redirect hop is re-validated, that the connection goes to the
  validated IP (so a later DNS answer cannot rebind it) and that TLS keeps the
  default verification settings.
* F2 - neither the download, the memory nor the parse work was bounded. The
  tests prove that a declared oversized body is refused without reading it, a
  streamed/compressed body is cut off at the cap without buffering it all, the
  HTML handed to the parser is capped, and the fetch is cooperatively
  cancellable with a bounded number of in-flight fetches.

Every assertion here fails against the pre-fix ``requests.get`` implementation.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import components.tools.visit_web as visit_web_module  # noqa: E402
from components.tools.sites import model as site_model  # noqa: E402
from components.tools.sites import safe_fetch  # noqa: E402
from components.tools.sites.safe_fetch import (  # noqa: E402
    BlockedTargetError,
    FetchAbortedError,
    FetchError,
    ResponseTooLargeError,
)

PUBLIC_IP = "93.184.216.34"


# --------------------------------------------------------------------------- #
# Test HTTP server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        owner = self.server.owner
        owner.hits.append(self.path)
        responder = owner.responses.get(self.path)
        if responder is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        responder(self)

    def log_message(self, *args):  # keep the test output quiet
        pass


class _Server:
    """Local HTTP server whose per-path behaviour is set by the test."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.hits: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


def _send(handler, body: bytes, *, status: int = 200, ctype: str = "text/html; charset=utf-8", headers=()):
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _route_connections(monkeypatch, port: int, attempted: list | None = None):
    """Send every validated connection to the local test server.

    The address the client decided to use is still recorded (``attempted``),
    so a test can prove which target passed validation.
    """
    real_create_connection = socket.create_connection

    def fake_create_connection(address, *args, **kwargs):
        if attempted is not None:
            attempted.append(address)
        return real_create_connection(("127.0.0.1", port), *args, **kwargs)

    monkeypatch.setattr(safe_fetch.socket, "create_connection", fake_create_connection)


def _connect_to(port: int):
    """Replacement for ``socket.create_connection`` aimed at the test server."""
    real_create_connection = socket.create_connection

    def connect(address, *args, **kwargs):
        return real_create_connection(("127.0.0.1", port), *args, **kwargs)

    return connect


def _fetch(server: "_Server", path: str, **kwargs):
    """Fetch ``path`` from the test server through a validated public URL."""
    with mock.patch.object(safe_fetch.socket, "create_connection", _connect_to(server.port)):
        return safe_fetch.fetch_document(f"http://{PUBLIC_IP}:{server.port}{path}", **kwargs)


def _tool() -> object:
    return visit_web_module.VisitWeb()


# --------------------------------------------------------------------------- #
# F1 - target validation
# --------------------------------------------------------------------------- #
def test_loopback_literal_is_refused_before_any_connection():
    with _Server() as server:
        server.responses["/"] = lambda handler: _send(handler, b"<html>ok</html>")

        with pytest.raises(BlockedTargetError):
            safe_fetch.fetch_document(f"http://127.0.0.1:{server.port}/")

        assert server.hits == []


def test_visit_web_refuses_a_loopback_target_end_to_end():
    with _Server() as server:
        server.responses["/"] = lambda handler: _send(handler, b"<html>ok</html>")

        result = asyncio.run(_tool().call({"url": f"http://127.0.0.1:{server.port}/"}))

        assert isinstance(result, str) and result.startswith("error visit web:")
        assert "refusing non-public address" in result
        assert server.hits == []


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",
        "169.254.169.254",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "[::1]",
        "[fd00::1]",
        "[fe80::1]",
        "[::ffff:127.0.0.1]",
    ],
)
def test_non_public_targets_are_refused_before_connecting(host: str, monkeypatch):
    attempted: list = []

    def fail_connect(*args, **kwargs):
        attempted.append(args)
        pytest.fail("the client must not open a connection for a blocked target")

    monkeypatch.setattr(safe_fetch.socket, "create_connection", fail_connect)

    with pytest.raises(BlockedTargetError):
        safe_fetch.fetch_document(f"http://{host}/")

    assert attempted == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "data:text/html,x"])
def test_non_http_schemes_are_refused(url: str, monkeypatch):
    monkeypatch.setattr(
        safe_fetch.socket, "create_connection", lambda *a, **k: pytest.fail("no connection expected")
    )

    with pytest.raises(BlockedTargetError):
        safe_fetch.fetch_document(url)


@pytest.mark.parametrize("url", ["http://[::1/", "http://", "https:///path"])
def test_malformed_urls_are_refused(url: str, monkeypatch):
    monkeypatch.setattr(
        safe_fetch.socket, "create_connection", lambda *a, **k: pytest.fail("no connection expected")
    )

    with pytest.raises(BlockedTargetError):
        safe_fetch.fetch_document(url)


def test_redirect_to_private_address_is_refused_and_never_connected(monkeypatch):
    attempted: list = []
    with _Server() as server:
        server.responses["/start"] = lambda handler: _send(
            handler,
            b"",
            status=302,
            headers=[("Location", "http://169.254.169.254/latest/meta-data/")],
        )
        _route_connections(monkeypatch, server.port, attempted)

        with pytest.raises(BlockedTargetError) as excinfo:
            safe_fetch.fetch_document(f"http://{PUBLIC_IP}:{server.port}/start")

        assert "169.254.169.254" in str(excinfo.value)
        assert server.hits == ["/start"]
        # Only the validated first hop was dialled; the redirect target was not.
        assert [address[0] for address in attempted] == [PUBLIC_IP]


def test_connection_uses_the_validated_ip_so_dns_cannot_rebind(monkeypatch):
    resolutions: list[str] = []

    def fake_getaddrinfo(host, port, **kwargs):
        resolutions.append(host)
        address = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    attempted: list = []

    def fake_create_connection(address, *args, **kwargs):
        attempted.append(address)
        raise OSError("unreachable in this test")

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(safe_fetch.socket, "create_connection", fake_create_connection)

    with pytest.raises(FetchError):
        safe_fetch.fetch_document("http://rebind.example/")

    # The socket went to the address that was validated, never to the second
    # (private) answer a rebinding resolver would hand out.
    assert attempted == [("93.184.216.34", 80)]


def test_tls_uses_the_default_verification_context(monkeypatch):
    created: list = []
    hostnames: list = []
    real_context = safe_fetch.ssl.create_default_context

    class _RecordingContext:
        def __init__(self, context):
            self.context = context

        def wrap_socket(self, sock, server_hostname=None, **kwargs):
            hostnames.append(server_hostname)
            return sock

    def fake_create_default_context(*args, **kwargs):
        context = real_context(*args, **kwargs)
        created.append(context)
        return _RecordingContext(context)

    with _Server() as server:
        server.responses["/"] = lambda handler: _send(handler, b"<html>secure</html>")
        _route_connections(monkeypatch, server.port)
        monkeypatch.setattr(safe_fetch.ssl, "create_default_context", fake_create_default_context)

        document = safe_fetch.fetch_document(f"https://{PUBLIC_IP}:{server.port}/")

        assert document.status_code == 200

    assert created and created[0].verify_mode == ssl.CERT_REQUIRED
    assert created[0].check_hostname is True
    assert hostnames == [PUBLIC_IP]


def test_public_addresses_are_recognised():
    assert safe_fetch.is_public_address("8.8.8.8")
    assert safe_fetch.is_public_address("93.184.216.34")
    assert safe_fetch.is_public_address("2606:4700::1111")
    assert not safe_fetch.is_public_address("127.0.0.1")
    assert not safe_fetch.is_public_address("169.254.169.254")
    assert not safe_fetch.is_public_address("::ffff:127.0.0.1")
    assert not safe_fetch.is_public_address("example.com")


# --------------------------------------------------------------------------- #
# F2 - bounded download, memory and parse work
# --------------------------------------------------------------------------- #
def test_oversized_declared_body_is_rejected_without_reading_it(monkeypatch):
    def huge_body(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", "100000000")
        handler.end_headers()
        handler.wfile.write(b"<html>")
        handler.wfile.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("the body must not be read once Content-Length exceeds the cap")

    monkeypatch.setattr(safe_fetch, "_raw_chunks", forbidden)

    with _Server() as server:
        server.responses["/huge"] = huge_body
        _route_connections(monkeypatch, server.port)

        with pytest.raises(ResponseTooLargeError) as excinfo:
            safe_fetch.fetch_document(f"http://{PUBLIC_IP}:{server.port}/huge")

    assert "100000000" in str(excinfo.value)


def test_streamed_body_is_capped_without_buffering_it_all():
    pulled: list[int] = []

    def chunks():
        for index in range(100):
            pulled.append(index)
            yield b"A" * (1024 * 1024)

    with pytest.raises(ResponseTooLargeError):
        safe_fetch.collect_bounded(chunks(), 8192)

    # One chunk is already over the cap: the remaining 99 MiB are never pulled.
    assert pulled == [0]


def test_chunked_response_is_capped():
    def chunked_body(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        block = b"B" * (256 * 1024)
        try:
            for _ in range(40):  # 10 MiB with no Content-Length to pre-check
                handler.wfile.write(b"%X\r\n" % len(block) + block + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    with _Server() as server:
        server.responses["/chunked"] = chunked_body
        # A small cap keeps the test quick while proving the cumulative bound.
        with pytest.raises(ResponseTooLargeError):
            _fetch(server, "/chunked", max_bytes=512 * 1024)


def test_compressed_body_is_capped_after_inflation():
    bomb = gzip.compress(b"A" * (8 * 1024 * 1024))
    assert len(bomb) < safe_fetch.MAX_RESPONSE_BYTES  # the wire form passes the byte cap

    with _Server() as server:
        server.responses["/bomb"] = lambda handler: _send(
            handler, bomb, headers=[("Content-Encoding", "gzip")]
        )
        with pytest.raises(ResponseTooLargeError) as excinfo:
            _fetch(server, "/bomb")

    assert "decompressed" in str(excinfo.value)


def test_gzip_inflation_stops_before_the_whole_stream_is_read():
    blob = gzip.compress(b"A" * (8 * 1024 * 1024))
    blocks = [blob[index : index + 4096] for index in range(0, len(blob), 4096)]
    pulled: list[bytes] = []

    def source():
        for block in blocks:
            pulled.append(block)
            yield block

    decoder = safe_fetch._BodyDecoder("gzip", 8192)
    with pytest.raises(ResponseTooLargeError):
        safe_fetch.collect_bounded(decoder.decode(source()), 8192)

    assert 0 < len(pulled) < len(blocks)


def test_parse_input_is_capped(monkeypatch):
    body = ("<html><head><title>Big</title></head><body>" + "word " * 200000 + "</body></html>").encode()

    monkeypatch.setattr(safe_fetch, "MAX_PARSE_CHARS", 4096)
    real_soup = site_model.BeautifulSoup
    parsed_sizes: list[int] = []

    def spy(markup, *args, **kwargs):
        parsed_sizes.append(len(markup))
        return real_soup(markup, *args, **kwargs)

    with _Server() as server:
        server.responses["/big"] = lambda handler: _send(handler, body)
        _route_connections(monkeypatch, server.port)
        url = f"http://{PUBLIC_IP}:{server.port}/big"

        document = safe_fetch.fetch_document(url)
        assert len(document.text) > 4096  # the page really is larger than the cap

        monkeypatch.setattr(site_model, "BeautifulSoup", spy)
        status_code, raw_html = site_model.SiteAdapterBase.get_html(url)
        site_model.SiteAdapterBase.extra_plain(raw_html)
        site_model.SiteAdapterBase.extra_title_element(raw_html)

    assert status_code == 200
    assert len(raw_html) == 4096
    assert parsed_sizes and max(parsed_sizes) <= 4096


def test_brief_len_is_clamped():
    assert site_model.clamp_brief_len(-10) == 1
    assert site_model.clamp_brief_len(0) == 1
    assert site_model.clamp_brief_len(1024) == 1024
    assert site_model.clamp_brief_len(10**9) == site_model.MAX_BRIEF_CHARS
    assert site_model.clamp_brief_len("nonsense") == site_model.DEFAULT_BRIEF_LEN
    assert site_model.clamp_brief_len(None) == site_model.DEFAULT_BRIEF_LEN


def test_fetch_deadline_stops_a_slow_peer():
    finished = threading.Event()

    def slow_body(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", "1000000")
        handler.end_headers()
        try:
            for _ in range(100):
                handler.wfile.write(b"x" * 1024)
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    errors: list[Exception] = []

    with _Server() as server:
        server.responses["/slow"] = slow_body
        with mock.patch.object(safe_fetch.socket, "create_connection", _connect_to(server.port)):
            def run():
                try:
                    safe_fetch.fetch_document(
                        f"http://{PUBLIC_IP}:{server.port}/slow", timeout=1.0
                    )
                except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                    errors.append(exc)
                finally:
                    finished.set()

            thread = threading.Thread(target=run, daemon=True)
            start = time.monotonic()
            thread.start()
            assert finished.wait(10), "the fetch did not honour its deadline"
            elapsed = time.monotonic() - start

    assert isinstance(errors[0], FetchAbortedError)
    assert elapsed < 5


def test_stop_event_aborts_an_in_flight_download():
    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []
    stop_event = threading.Event()

    def slow_body(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", "1000000")
        handler.end_headers()
        try:
            for _ in range(100):
                handler.wfile.write(b"x" * 1024)
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    with _Server() as server:
        server.responses["/slow"] = slow_body
        with mock.patch.object(safe_fetch.socket, "create_connection", _connect_to(server.port)):
            def run():
                started.set()
                try:
                    safe_fetch.fetch_document(
                        f"http://{PUBLIC_IP}:{server.port}/slow",
                        timeout=30,
                        stop_event=stop_event,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    finished.set()

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            assert started.wait(5)
            time.sleep(0.4)
            stop_event.set()

    assert finished.wait(10), "the stop flag did not interrupt the in-flight download"
    assert isinstance(errors[0], FetchAbortedError)


def test_cancelling_the_tool_sets_the_worker_stop_flag():
    observed: list[bool] = []
    done = threading.Event()

    def fake_process(url, brief_len, **kwargs):
        stop = kwargs["stop_event"]
        while not stop.is_set():
            time.sleep(0.01)
        observed.append(stop.is_set())
        done.set()
        return "aborted"

    async def scenario():
        with mock.patch.object(visit_web_module, "process", fake_process):
            task = asyncio.create_task(_tool().call({"url": "https://slow.example/"}))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert asyncio.run(asyncio.to_thread(done.wait, 5)), "worker never observed the cancellation"
    assert observed == [True]


def test_concurrent_fetches_are_bounded(monkeypatch):
    monkeypatch.setattr(visit_web_module, "FETCH_SLOT_WAIT_SECONDS", 0.25)
    gate = threading.Event()
    slots = visit_web_module.MAX_CONCURRENT_FETCHES

    def fake_process(url, brief_len, **kwargs):
        gate.wait(10)
        return f"done:{url}"

    async def scenario():
        with mock.patch.object(visit_web_module, "process", fake_process):
            tasks = [
                asyncio.create_task(_tool().call({"url": f"https://example.com/{index}"}))
                for index in range(slots + 1)
            ]
            await asyncio.sleep(0.3)  # let the first `slots` calls take the pool
            busy = await tasks[-1]
            gate.set()
            return busy, await asyncio.gather(*tasks[:-1])

    busy, finished = asyncio.run(scenario())

    assert busy == "error visit web: too many concurrent page fetches, please retry later"
    assert finished == [f"done:https://example.com/{index}" for index in range(slots)]
