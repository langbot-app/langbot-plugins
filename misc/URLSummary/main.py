import os
import re
import sys
import asyncio
import logging
import threading
import weakref
from html.parser import HTMLParser

from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.provider.message import Message, ContentElement

# Allow importing the plugin-level safe_fetch module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_fetch  # noqa: E402

MAX_CONTENT_CHARS = 100_000
"""Hard cap on the characters extracted from one page."""

MAX_TITLE_CHARS = 500
"""Hard cap on the extracted page title."""

MAX_CONCURRENT_FETCHES = 4
"""Upper bound on page fetches this plugin keeps in flight."""

FETCH_SLOT_WAIT_SECONDS = 10.0
"""How long a fetch may wait for a free slot before it is rejected."""


class _TextExtractor(HTMLParser):
    """Simple HTML to text extractor."""

    def __init__(self):
        super().__init__()
        self._text = []
        self._skip = False
        self._skip_tags = {'script', 'style', 'noscript', 'header', 'footer', 'nav'}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False
        if tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'):
            self._text.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self) -> str:
        return re.sub(r'\n{3,}', '\n\n', ''.join(self._text)).strip()


URL_PATTERN = re.compile(r'https?://[^\s<>\]\)]+')

LANG_MAP = {
    'zh_Hans': '请用简体中文回复',
    'en_US': 'Please reply in English',
    'ja_JP': '日本語で回答してください',
}


_slots_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_slots_guard = threading.Lock()


def _slots() -> asyncio.Semaphore:
    """Return the bounded fetch-slot pool of the running event loop.

    ``asyncio.Semaphore`` binds to the loop that first waits on it, while the
    shared runtime and the test suite each drive their own loop, so the pool is
    keyed by loop. It holds no tenant data.
    """
    loop = asyncio.get_running_loop()
    with _slots_guard:
        slots = _slots_by_loop.get(loop)
        if slots is None:
            slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
            _slots_by_loop[loop] = slots
        return slots


def _guarded_fetch(
    url: str,
    max_len: int,
    stop_event: threading.Event,
    slots: asyncio.Semaphore,
    loop: asyncio.AbstractEventLoop,
) -> tuple[str, str]:
    """Run the blocking fetch and release the slot when the thread really ends."""
    try:
        return _fetch_and_extract(url, max_len, stop_event)
    finally:
        try:
            loop.call_soon_threadsafe(slots.release)
        except RuntimeError:
            # The loop is gone (plugin unloaded); nothing left to bound.
            pass


def _fetch_and_extract(url: str, max_len: int, stop_event: threading.Event | None = None) -> tuple[str, str]:
    """Download one page under the safe_fetch rules and extract its text."""
    document = safe_fetch.fetch_document(
        url,
        stop_event=stop_event,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; LangBot-URLSummary/1.0)'},
    )

    if document.status_code != 200:
        raise Exception(f"HTTP {document.status_code}")
    content_type = document.content_type
    if 'text/html' not in content_type and 'application/xhtml' not in content_type:
        raise Exception(f"Not HTML: {content_type}")

    # Bound the markup handed to the parser, not only the extracted output.
    html = document.text[: safe_fetch.MAX_PARSE_CHARS]

    title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip()[:MAX_TITLE_CHARS] if title_match else url

    extractor = _TextExtractor()
    extractor.feed(html)
    text = extractor.get_text()

    return title, text[:min(max_len, MAX_CONTENT_CHARS)]


def redact_url(url: str) -> str:
    """Drop credentials and the query string before a URL is logged."""
    parts = re.split(r'([?#])', url, maxsplit=1)[0]
    parts = re.sub(r'//[^/@]*@', '//', parts)
    return parts


class URLSummary(BasePlugin):

    async def initialize(self):
        self.logger = logging.getLogger("URLSummary")
        self.logger.info("URLSummary plugin initialized")

    async def fetch_page(self, url: str, max_len: int) -> tuple[str, str]:
        """Fetch a web page and return (title, text_content).

        The blocking, target-validated download and the synchronous HTML parse
        run in a worker thread, so the shared event loop keeps serving other
        tenants and never parses more than ``safe_fetch.MAX_PARSE_CHARS`` of
        markup. Fetch concurrency is bounded per process; a call that waits
        longer than ``FETCH_SLOT_WAIT_SECONDS`` is rejected instead of queueing
        without limit.
        """
        slots = _slots()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=FETCH_SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise Exception("too many concurrent page fetches, please retry later") from exc

        loop = asyncio.get_running_loop()
        stop_event = threading.Event()
        try:
            return await asyncio.to_thread(_guarded_fetch, url, max_len, stop_event, slots, loop)
        except asyncio.CancelledError:
            # The worker cannot be interrupted once it is inside the socket
            # read; the flag makes it stop at the next chunk boundary.
            stop_event.set()
            raise

    async def summarize(self, url: str, title: str, content: str, model_uuid: str, language: str) -> str:
        """Use LLM to summarize the page content."""
        lang_instruction = LANG_MAP.get(language, LANG_MAP['zh_Hans'])

        prompt = f"""{lang_instruction}。

请总结以下网页内容，生成简洁的摘要。包含关键信息和要点。

网页标题: {title}
网页链接: {url}

网页内容:
{content}"""

        msg = Message(
            role="user",
            content=[ContentElement.from_text(prompt)],
        )

        response = await self.invoke_llm(
            messages=[msg],
            llm_model_uuid=model_uuid,
        )

        if isinstance(response.content, str):
            return response.content
        elif isinstance(response.content, list):
            parts = []
            for elem in response.content:
                if hasattr(elem, 'text'):
                    parts.append(elem.text)
            return ''.join(parts)
        return str(response.content)
