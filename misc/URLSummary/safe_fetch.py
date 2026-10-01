"""Target-validated, byte-bounded HTTP(S) fetching for the URLSummary listener.

Why this module exists
----------------------
The listener summarises URLs that any chat participant can post, and it runs
inside the shared runtime, where one process serves every tenant. The previous
``aiohttp`` call had two blocking problems (certification review F1/F2):

* the caller picked the server-side network target - loopback, link-local
  metadata (``169.254.169.254``), private ranges, or a public host that
  redirects to one of them - and TLS verification was switched off
  (``ssl=False``), so an on-path attacker could replace the page that is fed to
  the model;
* the whole response was read and parsed before ``max_content_length``
  truncated the *output*, so a hostile server could occupy shared memory and
  the shared event loop without bound.

This module therefore:

* accepts only ``http``/``https`` URLs;
* resolves the host itself, refuses every resolution that contains a
  non-public address, and then connects to the *validated IP literal* - a
  later, different DNS answer can no longer rebind the connection;
* follows redirects manually, re-running the same validation on every hop;
* reads the body in bounded chunks with a hard cap on the decoded bytes, a
  total deadline and a cooperative stop flag, and closes the socket on every
  exit path;
* enforces that deadline and stop flag on *every* blocking step - DNS, connect,
  TLS handshake, status/header parsing and body reads - by shutting the socket
  down from a watchdog thread, because a socket timeout only bounds the idle
  time between two reads and a peer that drips bytes can outlive it forever;
* opens the socket directly, so no proxy environment variables or netrc
  credentials are consulted;
* verifies TLS with the default CA store and the original hostname (the
  pinned IP is only the transport target);
* never puts a raw URL back into an error message: :func:`redact_url` drops the
  userinfo, query string and fragment, and the same scrub is applied to
  chained exception text so a traceback cannot leak credentials either.
"""

from __future__ import annotations

import base64
import dataclasses
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
"""Hard cap on the decoded response body kept for one fetch."""

MAX_PARSE_CHARS = 256 * 1024
"""Cap on the HTML text handed to BeautifulSoup."""

MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 5.0
TOTAL_TIMEOUT = 15.0

_CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REQUEST_DEFAULTS = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}


class FetchError(Exception):
    """Base class for every failure raised by this module."""


class BlockedTargetError(FetchError):
    """The URL or one of its redirects points at a forbidden target."""


class ResponseTooLargeError(FetchError):
    """The response body exceeded the configured byte cap."""


class FetchAbortedError(FetchError):
    """The fetch was cancelled or ran past its deadline."""


class TooManyRedirectsError(FetchError):
    """The redirect chain is longer than :data:`MAX_REDIRECTS`."""


def _strip_url_tail(value: str) -> str:
    """Fallback scrub for a URL that :func:`urlsplit` cannot parse."""
    head = re.split(r"[?#]", value, maxsplit=1)[0]
    return re.sub(r"//[^/@]*@", "//", head)


def redact_url(url: str) -> str:
    """Return ``url`` without userinfo, query string or fragment.

    This is the single redaction used by every message and log line of this
    plugin, so a password embedded in the URL or a signed query token can never
    be written back out.
    """
    try:
        parts = urlsplit(str(url))
        host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            # A non-numeric port makes ``port`` raise; the host is still known.
            port = None
    except (AttributeError, ValueError):
        return _strip_url_tail(str(url))

    if not host:
        return _strip_url_tail(str(url))

    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>()\[\],;]+")


def redact_text(text: str) -> str:
    """Redact credentials/query/fragment from every URL inside ``text``.

    Used as the last boundary before exception text (or a traceback built from
    it) is handed to a logger, so a URL that reached a chained cause or a
    server-controlled header is still scrubbed.
    """
    if not text:
        return text
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), str(text))


def _scrub_exception(exc: BaseException | None) -> BaseException | None:
    """Strip credentials/query/fragment from an exception's own text.

    ``raise ... from exc`` keeps ``exc`` in the traceback, so the chained
    exception has to be scrubbed before it is re-raised.
    """
    if exc is None:
        return None
    try:
        if exc.args:
            exc.args = tuple(
                redact_text(arg) if isinstance(arg, str) else arg for arg in exc.args
            )
    except Exception:  # pragma: no cover - exotic exception objects
        pass
    return exc


class _AbortWatch:
    """End a blocking socket operation at the deadline or on cancellation.

    ``socket.settimeout`` only bounds the idle time *between* two reads, so a
    peer that drips one byte before every timeout can keep ``getresponse()``
    (status and header parsing) or the TLS handshake blocked indefinitely.
    Shutting the socket down from a helper thread makes every blocked socket
    call return, which is what lets the worker thread end and give its fetch
    slot back.
    """

    _POLL_SECONDS = 0.05

    def __init__(self, deadline: float, stop_event: Any) -> None:
        self._deadline = deadline
        self._stop_event = stop_event
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._triggered = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="safe-fetch-abort", daemon=True
        )

    @property
    def triggered(self) -> bool:
        """``True`` when this watch (not the peer) ended the socket."""
        return self._triggered.is_set()

    def attach(self, sock: socket.socket) -> None:
        """Start watching ``sock``; call once, before its first blocking use."""
        with self._lock:
            self._sock = sock
        self._thread.start()

    def _watch(self) -> None:
        while not self._finished.wait(self._POLL_SECONDS):
            if self._stop_event is not None and self._stop_event.is_set():
                self._triggered.set()
                self._abort()
                return
            if time.monotonic() >= self._deadline:
                self._triggered.set()
                self._abort()
                return

    def _abort(self) -> None:
        with self._lock:
            sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def stop(self) -> None:
        """Stop watching; safe to call on every exit path."""
        self._finished.set()
        if self._thread.is_alive():
            self._thread.join(1.0)


@dataclasses.dataclass(frozen=True)
class Document:
    """A bounded, decoded HTTP response."""

    status_code: int
    url: str
    content_type: str
    text: str


@dataclasses.dataclass(frozen=True)
class _RawResponse:
    status_code: int
    headers: Any
    body: bytes
    charset: str | None


def is_public_address(value: str) -> bool:
    """Return ``True`` only for addresses that are globally routable."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 is loopback, not a public IPv6 address.
        address = address.ipv4_mapped
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return address.is_global


def resolve_public_addresses(
    host: str, port: int, deadline: float | None = None, stop_event: Any = None
) -> list[str]:
    """Resolve ``host`` and return its addresses, refusing non-public ones.

    With a ``deadline`` the lookup runs on a helper thread and this call gives
    up as soon as the deadline expires or ``stop_event`` is set: a resolver
    that never answers is another way to pin a fetch slot forever.
    """
    infos = _getaddrinfo(host, port, deadline, stop_event)

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if not is_public_address(address):
            raise BlockedTargetError(
                f"refusing non-public address {address!r} for host {host!r}"
            )
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise BlockedTargetError(f"host {host!r} resolved to no address")
    return addresses


def _getaddrinfo(host: str, port: int, deadline: float | None, stop_event: Any) -> list:
    """Run ``getaddrinfo``, optionally bounded by a deadline/stop flag."""
    if deadline is None:
        try:
            return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise BlockedTargetError(f"cannot resolve host {host!r}: {exc}") from exc

    result: dict[str, Any] = {}

    def lookup() -> None:
        try:
            result["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            result["error"] = exc

    thread = threading.Thread(target=lookup, name="safe-fetch-dns", daemon=True)
    thread.start()
    while thread.is_alive():
        if stop_event is not None and stop_event.is_set():
            raise FetchAbortedError("fetch cancelled while resolving host")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchAbortedError("fetch deadline exceeded while resolving host")
        thread.join(min(0.05, remaining))

    error = result.get("error")
    if error is not None:
        raise BlockedTargetError(
            f"cannot resolve host {host!r}: {redact_text(str(error))}"
        ) from _scrub_exception(error)
    return result["infos"]


def validate_url(url: str) -> tuple[str, str, int, str]:
    """Validate scheme/host/port and return ``(scheme, host, port, path)``."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BlockedTargetError(
            f"invalid URL: {redact_url(url)!r}"
        ) from _scrub_exception(exc)
    scheme = (parts.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        raise BlockedTargetError(f"refusing non-HTTP(S) URL: {redact_url(url)!r}")
    host = parts.hostname
    if not host:
        raise BlockedTargetError(f"URL has no host: {redact_url(url)!r}")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise BlockedTargetError(
            f"invalid port in URL: {redact_url(url)!r}"
        ) from _scrub_exception(exc)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


def fetch_document(
    url: str,
    *,
    timeout: float = TOTAL_TIMEOUT,
    stop_event: Any = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    headers: Mapping[str, str] | None = None,
) -> Document:
    """Fetch ``url`` under the validation and size rules of this module.

    ``stop_event`` is an optional :class:`threading.Event`; when set, the fetch
    aborts at the next blocking step. ``timeout`` is the deadline for the whole
    operation, redirects included, and it is enforced on DNS, connect, the TLS
    handshake, status/header parsing and the body.
    """
    deadline = time.monotonic() + timeout
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, path = validate_url(current)
        _check_abort(stop_event, deadline)
        addresses = resolve_public_addresses(host, port, deadline, stop_event)
        _check_abort(stop_event, deadline)

        raw = _request_once(
            scheme, host, port, path, addresses, current, deadline, stop_event, max_bytes, headers
        )

        if raw.status_code in _REDIRECT_STATUSES:
            location = raw.headers.get("Location")
            if location:
                current = urljoin(current, location)
                continue

        return Document(
            status_code=raw.status_code,
            url=current,
            content_type=raw.headers.get("Content-Type", "") or "",
            text=_decode_text(raw.body, raw.charset),
        )

    raise TooManyRedirectsError(
        f"more than {MAX_REDIRECTS} redirects from {redact_url(url)!r}"
    )


def _check_abort(stop_event: Any, deadline: float) -> None:
    if stop_event is not None and stop_event.is_set():
        raise FetchAbortedError("fetch cancelled")
    if time.monotonic() >= deadline:
        raise FetchAbortedError("fetch deadline exceeded")


def _remaining(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _request_once(
    scheme: str,
    host: str,
    port: int,
    path: str,
    addresses: list[str],
    url: str,
    deadline: float,
    stop_event: Any,
    max_bytes: int,
    headers: Mapping[str, str] | None,
) -> _RawResponse:
    sock = None
    last_error: OSError | None = None
    for address in addresses:
        # A stalled connect must not outlive the deadline or the cancellation.
        _check_abort(stop_event, deadline)
        try:
            sock = socket.create_connection(
                (address, port), timeout=min(CONNECT_TIMEOUT, _remaining(deadline))
            )
            break
        except OSError as exc:
            last_error = exc
    if sock is None:
        _check_abort(stop_event, deadline)
        raise FetchError(
            f"cannot connect to {redact_url(url)!r}: {redact_text(str(last_error))}"
        )

    connection = None
    watch = _AbortWatch(deadline, stop_event)
    try:
        sock.settimeout(min(READ_TIMEOUT, _remaining(deadline)))
        if scheme == "https":
            context = ssl.create_default_context()
            # The socket goes to the validated IP, the certificate and SNI stay
            # bound to the requested host name.  The handshake is deferred so
            # the abort watch can cover it as well.
            sock = context.wrap_socket(
                sock, server_hostname=host, do_handshake_on_connect=False
            )
        watch.attach(sock)
        if scheme == "https":
            sock.do_handshake()

        connection = http.client.HTTPConnection(host, port)
        connection.sock = sock
        connection.request("GET", path, headers=_request_headers(url, headers))
        response = connection.getresponse()

        status_code = response.status
        response_headers = response.headers
        if status_code in _REDIRECT_STATUSES and response_headers.get("Location"):
            # The body of a redirect is not needed; dropping the connection
            # discards it without reading it.
            return _RawResponse(status_code, response_headers, b"", None)

        declared = response.length
        if declared is not None and declared > max_bytes:
            raise ResponseTooLargeError(
                f"Content-Length {declared} exceeds the {max_bytes}-byte limit for "
                f"{redact_url(url)!r}"
            )

        decoder = _BodyDecoder(response_headers.get("Content-Encoding"), max_bytes)
        chunks = decoder.decode(chunk for chunk in _raw_chunks(response, sock, stop_event, deadline))
        body = collect_bounded(chunks, max_bytes)
        charset = response_headers.get_content_charset()
        return _RawResponse(status_code, response_headers, body, charset)
    except FetchError:
        raise
    except (http.client.HTTPException, OSError, zlib.error) as exc:
        if watch.triggered:
            # The watch shut the socket down: the fetch was cancelled or ran
            # past its deadline, not a peer failure.
            raise FetchAbortedError(
                "fetch cancelled or deadline exceeded"
            ) from _scrub_exception(exc)
        raise FetchError(
            f"request to {redact_url(url)!r} failed: {redact_text(str(exc))}"
        ) from _scrub_exception(exc)
    finally:
        watch.stop()
        if connection is not None:
            connection.close()
        else:
            sock.close()


def _request_headers(url: str, extra: Mapping[str, str] | None) -> dict[str, str]:
    headers = dict(_REQUEST_DEFAULTS)
    if extra:
        headers.update(extra)
    parts = urlsplit(url)
    if parts.username:
        # Preserve the previous requests-based behaviour for URLs that carry
        # credentials, without consulting netrc or the environment.
        credentials = unquote(parts.username)
        if parts.password:
            credentials += ":" + unquote(parts.password)
        token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def _raw_chunks(
    response: http.client.HTTPResponse, sock: socket.socket, stop_event: Any, deadline: float
) -> Iterator[bytes]:
    """Yield the raw (still encoded) body in bounded chunks."""
    while True:
        _check_abort(stop_event, deadline)
        sock.settimeout(min(READ_TIMEOUT, _remaining(deadline)))
        try:
            chunk = response.read1(_CHUNK_SIZE)
        except socket.timeout as exc:
            raise FetchAbortedError(f"response read timed out: {exc}") from exc
        if not chunk:
            return
        yield chunk


def collect_bounded(chunks: Iterable[bytes], limit: int) -> bytes:
    """Join ``chunks`` while refusing to buffer more than ``limit`` bytes.

    The check happens *before* a chunk is appended and before the next chunk is
    pulled, so an oversized body is detected without reading it all.
    """
    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        if len(buffer) + len(chunk) > limit:
            raise ResponseTooLargeError(f"response exceeds the {limit}-byte limit")
        buffer.extend(chunk)
    return bytes(buffer)


class _BodyDecoder:
    """Streaming gzip/deflate decoder with a cap on the inflated size."""

    def __init__(self, encoding: str | None, limit: int) -> None:
        self.encoding = (encoding or "").strip().lower()
        self._limit = limit
        if self.encoding in ("", "identity"):
            self._inflater = None
        elif self.encoding == "gzip":
            self._inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif self.encoding == "deflate":
            self._inflater = zlib.decompressobj()
        else:
            raise FetchError(f"unsupported Content-Encoding: {self.encoding!r}")

    def decode(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        if self._inflater is None:
            yield from chunks
            return
        for chunk in chunks:
            out = self._inflater.decompress(chunk, self._limit)
            if self._inflater.unconsumed_tail:
                # More inflated bytes are pending than the cap allows: stop
                # before the decompression bomb can grow.
                raise ResponseTooLargeError(
                    f"decompressed response exceeds the {self._limit}-byte limit"
                )
            if out:
                yield out
        tail = self._inflater.flush()
        if tail:
            yield tail


def _decode_text(body: bytes, charset: str | None) -> str:
    encoding = charset or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
