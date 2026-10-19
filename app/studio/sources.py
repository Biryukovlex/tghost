"""SSRF-safe, bounded source retrieval for Studio research.

This is intentionally a reader, not a crawler. It follows only a small number
of explicit HTTP redirects, validates DNS/IP policy at every hop, streams at a
hard byte limit, and retains a short extracted excerpt plus provenance. Source
text is untrusted data: likely prompt-injection instructions are removed from
the model-facing excerpt and recorded as a warning flag.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from html.parser import HTMLParser

from .. import limits
from .search import canonicalize_url

_ALLOWED_MIME = {"text/html", "application/xhtml+xml", "text/plain"}
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous_instructions", re.compile(r"\b(ignore|disregard|forget|игнорируй|забудь)\b.{0,80}\b(previous|prior|earlier|all|any|предыдущие|прошлые|все)\b.{0,50}\b(instructions?|rules?|messages?|инструкции|правила|сообщения)\b", re.I | re.S)),
    ("system_prompt_impersonation", re.compile(r"\b(system|developer|assistant|системный)\s+(message|prompt|instruction|промпт|инструкция)\b", re.I)),
    ("tool_manipulation", re.compile(r"\b(call|use|execute|invoke|вызови|используй)\s+(the\s+)?(tool|function|browser|terminal|инструмент|функцию)\b", re.I)),
    ("secret_exfiltration", re.compile(r"\b(reveal|disclose|print|leak|expose|раскрой|покажи)\b.{0,50}\b(secret|password|token|prompt|credential|секрет|пароль|токен|промпт)\b", re.I)),
    ("jailbreak_language", re.compile(r"\b(jailbreak|override|do not follow|new instructions|новые\s+инструкции)\b", re.I)),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit].strip()


def _clean_lines(value: Any, limit: int) -> str:
    """Normalize text while retaining line boundaries for injection filtering."""

    lines = [" ".join(line.replace("\x00", " ").split()) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)[:limit].strip()


def _mime(content_type: str) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


_EXTRA_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),   # shared address space (CGNAT, Tailscale)
    ipaddress.ip_network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.ip_network("64:ff9b::/96"),    # NAT64 well-known prefix
)


def _blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    # IPv4-mapped IPv6 literals must be checked as their IPv4 address too.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    # ``is_global`` is the broad test (it excludes documentation, benchmarking
    # and other non-routable space that ``is_private`` does not). The explicit
    # networks below are ranges some Python versions still report as global.
    if not address.is_global:
        return True
    for network in _EXTRA_BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return True
    # ipaddress.is_private covers RFC1918 and many reserved ranges, but keep
    # each category explicit because these are separate SSRF threat classes.
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address in {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("100.100.100.200")}
    )


class SourceReaderError(RuntimeError):
    """A safe, public classification for a source-reader policy failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class SSRFBlockedError(SourceReaderError):
    pass


class SourcePolicyError(SourceReaderError):
    pass


@dataclass(frozen=True, slots=True)
class SourceDocument:
    url: str
    canonical_url: str
    final_url: str
    status: str = "ok"  # ok, inaccessible, blocked, unsupported
    accessible: bool = True
    status_code: int | None = None
    title: str = ""
    text: str = ""
    excerpt: str = ""
    content_type: str = ""
    bytes_read: int = 0
    redirects: int = 0
    fetched_at: datetime = field(default_factory=_now)
    source_hash: str = ""
    source_id: str = ""
    injection_flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "final_url": self.final_url,
            "status": self.status,
            "accessible": self.accessible,
            "status_code": self.status_code,
            "title": self.title,
            "text": self.text,
            "excerpt": self.excerpt,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "redirects": self.redirects,
            "fetched_at": _iso(self.fetched_at),
            "source_hash": self.source_hash,
            "source_id": self.source_id,
            "injection_flags": list(self.injection_flags),
            "warnings": list(self.warnings),
            "provenance": dict(self.provenance),
        }

    as_dict = model_dump


class _HTMLTextExtractor(HTMLParser):
    """Tiny non-executing HTML extractor with title handling."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title" and self._skip_depth == 0:
            self._in_title = True
        if tag in {"p", "div", "br", "li", "article", "section", "h1", "h2", "h3"} and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "br", "li", "article", "section", "h1", "h2", "h3"} and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def extracted(self) -> tuple[str, str]:
        title = _clean(" ".join(self.title_parts), 500)
        text = re.sub(r"\n\s*\n+", "\n", "".join(self.parts))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return title, _clean_lines(text, 10_000)


def sanitize_untrusted_text(text: str) -> tuple[str, tuple[str, ...]]:
    """Remove instruction-shaped lines from model-facing untrusted text.

    This filter is heuristic; the system prompt remains the primary defense.
    """

    flags: list[str] = []
    for flag, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            flags.append(flag)
    if not flags:
        return text, ()
    # Drop suspicious line-level instructions rather than handing them to the
    # model. Keep the remainder useful as factual source context.
    kept: list[str] = []
    for line in text.splitlines():
        if any(pattern.search(line) for _, pattern in _INJECTION_PATTERNS):
            continue
        kept.append(line)
    sanitized = "\n".join(kept).strip()
    return sanitized, tuple(dict.fromkeys(flags))


# Kept as a private compatibility alias for the original M4 call sites.
_sanitize_untrusted = sanitize_untrusted_text


Resolver = Callable[[str, int], Iterable[str] | Awaitable[Iterable[str]]]


class SafeSourceReader:
    """Read one source under strict URL, network, and content bounds."""

    def __init__(
        self,
        settings=None,
        *,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
        max_redirects: int | None = None,
        max_chars: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        allow_private_for_tests: bool = False,
    ) -> None:
        self.settings = settings
        self.enabled = limits.SOURCE_READER_ENABLED
        self.timeout_seconds = max(0.2, min(float(timeout_seconds if timeout_seconds is not None else limits.SOURCE_TIMEOUT_SECONDS), 60.0))
        self.max_bytes = max(1_024, min(int(max_bytes if max_bytes is not None else limits.SOURCE_MAX_BYTES), 10_000_000))
        self.max_redirects = max(0, min(int(max_redirects if max_redirects is not None else limits.SOURCE_MAX_REDIRECTS), 10))
        self.max_chars = max(200, min(int(max_chars if max_chars is not None else limits.SOURCE_MAX_CHARS), 100_000))
        self.transport = transport
        self.client = client
        self.resolver = resolver
        # This opt-in is only for local hostile-network fixtures. Production
        # callers must leave it false; it is never configurable by HTTP input.
        self.allow_private_for_tests = bool(allow_private_for_tests)

    def validate_url(self, value: str) -> str:
        """Validate a request URL without changing its routing path or query."""

        raw = str(value or "").strip()
        canonical = canonicalize_url(raw)
        if not canonical:
            raise SourcePolicyError("source_url_invalid", "Only credential-free HTTP and HTTPS source URLs are allowed.")
        parsed = urlsplit(canonical)
        if parsed.username or parsed.password or parsed.hostname is None:
            raise SSRFBlockedError("source_ssrf_blocked", "The source URL is not allowed.")
        # Canonicalization is for source identity, not HTTP routing. In
        # particular, sites may require the trailing slash we deduplicate.
        original = urlsplit(raw)
        return urlunsplit((parsed.scheme, parsed.netloc, original.path or "/", original.query, ""))

    async def _resolve_and_validate(self, url: str) -> str:
        canonical, _address = await self._resolve_target(url)
        return canonical

    async def _resolve_target(self, url: str) -> tuple[str, str | None]:
        canonical = self.validate_url(url)
        parsed = urlsplit(canonical)
        host = parsed.hostname or ""
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise SourcePolicyError("source_url_invalid", "The source URL has an invalid port.") from exc
        # A test transport may deliberately use loopback for an in-process
        # fixture, but the production default always resolves and checks.
        if self.allow_private_for_tests and self.transport is not None:
            return canonical, None
        try:
            if self.resolver is not None:
                resolved = self.resolver(host, port)
                ips = await resolved if isawaitable(resolved) else resolved
            else:
                infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
                ips = [str(info[4][0]) for info in infos]
        except (OSError, socket.gaierror) as exc:
            raise SSRFBlockedError("source_dns_failed", "The source host could not be resolved safely.") from exc
        addresses = list(dict.fromkeys(str(ip) for ip in (ips or [])))
        if not addresses or any(_blocked_ip(ip) for ip in addresses):
            raise SSRFBlockedError("source_ssrf_blocked", "The source host resolves to a private or reserved network.")
        return canonical, addresses[0]

    def _blocked_document(self, original: str, *, code: str, message: str, status: str = "blocked", status_code: int | None = None) -> SourceDocument:
        canonical = canonicalize_url(original)
        now = _now()
        return SourceDocument(
            url=str(original or ""),
            canonical_url=canonical,
            final_url=canonical,
            status=status,
            accessible=False,
            status_code=status_code,
            fetched_at=now,
            warnings=(message,),
            provenance={"status": status, "error_code": code, "retrieved_at": _iso(now),
                        **({"status_code": status_code} if status_code is not None else {})},
        )

    async def read(self, url: str) -> SourceDocument:
        """Return a safe bounded document; policy failures never trigger I/O."""

        original = str(url or "").strip()
        if not self.enabled:
            return self._blocked_document(original, code="source_reader_disabled", message="Source reading is disabled in this deployment.", status="inaccessible")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                canonical = self.validate_url(original)
                return await self._read_validated(original, canonical)
        except asyncio.TimeoutError:
            return self._blocked_document(original, code="source_timeout", message="The source exceeded the retrieval time limit.", status="inaccessible")
        except (httpx.HTTPError, OSError, TimeoutError):
            return self._blocked_document(original, code="source_unavailable", message="The source could not be retrieved.", status="inaccessible")
        except SourceReaderError as exc:
            return self._blocked_document(original, code=exc.code, message=exc.public_message)

    async def _read_validated(self, original: str, canonical: str) -> SourceDocument:
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
            headers={"User-Agent": "TG-Studio/1.0 (+self-hosted)"},
        )
        current = canonical
        redirects = 0
        try:
            while True:
                # Re-resolve every hop, including the first URL. A redirect to
                # a private address therefore cannot bypass the initial check.
                current, address = await self._resolve_target(current)
                parsed = urlsplit(current)
                # Connect to the validated IP, retaining the original HTTP
                # authority and TLS SNI/certificate hostname. No second DNS
                # lookup can rebind a public hostname to a private address.
                target = httpx.URL(current).copy_with(host=address) if address else httpx.URL(current)
                async with client.stream("GET", target, follow_redirects=False,
                                         headers={"Host": parsed.netloc},
                                         extensions={"sni_hostname": parsed.hostname}) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location", "")
                        if not location:
                            return self._blocked_document(original, code="source_redirect_invalid", message="The source returned an invalid redirect.", status="inaccessible")
                        if redirects >= self.max_redirects:
                            return self._blocked_document(original, code="source_redirect_limit", message="The source exceeded the redirect limit.", status="blocked")
                        current = urljoin(current, location)
                        redirects += 1
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        return self._blocked_document(original, code="source_http_error", message=f"The source returned HTTP {response.status_code}.", status="inaccessible", status_code=response.status_code)
                    content_type = _mime(response.headers.get("content-type", ""))
                    if content_type not in _ALLOWED_MIME:
                        return self._blocked_document(original, code="source_type_unsupported", message="The source content type is not supported.", status="unsupported")
                    content_length = response.headers.get("content-length")
                    try:
                        if content_length and int(content_length) > self.max_bytes:
                            return self._blocked_document(original, code="source_size_limit", message="The source exceeded the byte limit.", status="blocked")
                    except ValueError:
                        pass
                    body = bytearray()
                    digest = hashlib.sha256()
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        if len(body) + len(chunk) > self.max_bytes:
                            return self._blocked_document(original, code="source_size_limit", message="The source exceeded the byte limit.", status="blocked")
                        body.extend(chunk)
                        digest.update(chunk)
                    encoding = response.encoding or "utf-8"
                    try:
                        decoded = bytes(body).decode(encoding, errors="replace")
                    except LookupError:
                        decoded = bytes(body).decode("utf-8", errors="replace")
                    title = ""
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        parser = _HTMLTextExtractor()
                        try:
                            parser.feed(decoded)
                            parser.close()
                            title, decoded = parser.extracted()
                        except (ValueError, AssertionError):
                            decoded = _clean_lines(html.unescape(re.sub(r"<[^>]+>", " ", decoded)), self.max_chars)
                    else:
                        decoded = _clean_lines(decoded, self.max_chars)
                    safe_text, injection_flags = _sanitize_untrusted(decoded)
                    safe_title, title_flags = _sanitize_untrusted(title)
                    if title_flags:
                        title = ""
                        injection_flags = tuple(dict.fromkeys((*injection_flags, *title_flags)))
                    else:
                        title = safe_title
                    safe_text = safe_text[: self.max_chars]
                    if content_type in {"text/html", "application/xhtml+xml"} and safe_text.strip() in {"", title.strip()}:
                        return self._blocked_document(original, code="source_content_empty",
                            message="The page returned no readable article text; it may require JavaScript or a different source.",
                            status="inaccessible", status_code=response.status_code)
                    source_hash = digest.hexdigest()
                    final_canonical = canonicalize_url(current) or current
                    source_id = hashlib.sha256(f"{final_canonical}:{source_hash}".encode("utf-8")).hexdigest()[:24]
                    fetched_at = _now()
                    warnings = ("Source text contained instruction-like content; it was excluded from the model-facing excerpt.",) if injection_flags else ()
                    return SourceDocument(
                        url=original,
                        canonical_url=canonicalize_url(canonical),
                        final_url=final_canonical,
                        status="ok",
                        accessible=True,
                        status_code=response.status_code,
                        title=title,
                        text=safe_text,
                        excerpt=safe_text[:2_000],
                        content_type=content_type,
                        bytes_read=len(body),
                        redirects=redirects,
                        fetched_at=fetched_at,
                        source_hash=source_hash,
                        source_id=source_id,
                        injection_flags=injection_flags,
                        warnings=warnings,
                        provenance={
                            "retrieved_at": _iso(fetched_at),
                            "canonical_url": final_canonical,
                            "content_type": content_type,
                            "status_code": response.status_code,
                            "bytes_read": len(body),
                            "redirects": redirects,
                            "source_hash": source_hash,
                            "injection_flags": list(injection_flags),
                        },
                    )
        finally:
            if own_client:
                await client.aclose()


# Friendly aliases used by the spec and adapters.
SourceReader = SafeSourceReader
SafeSource = SourceDocument
is_safe_url = canonicalize_url
