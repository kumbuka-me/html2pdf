"""Small WSGI service that renders self-contained UTF-8 HTML to PDF."""

from __future__ import annotations

import hmac
import html
import ipaddress
import logging
import os
import re
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from email.message import Message
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO, TypeAlias, cast
from urllib.parse import urlsplit

from weasyprint import HTML
from weasyprint.urls import URLFetcher

Environ: TypeAlias = Mapping[str, Any]
Header: TypeAlias = tuple[str, str]
Headers: TypeAlias = list[Header]
Response: TypeAlias = list[bytes]
StartResponse: TypeAlias = Callable[[str, Headers], Any]
Application: TypeAlias = Callable[[Environ, StartResponse], Response]

logger = logging.getLogger("gunicorn.error")


class AssetPolicy(StrEnum):
    """Control which asset URL schemes submitted documents may fetch."""

    EMBEDDED = "embedded"
    REMOTE = "remote"

    @property
    def allowed_schemes(self) -> frozenset[str]:
        """Return URL schemes allowed by this policy."""
        if self is AssetPolicy.REMOTE:
            return frozenset(("data", "http", "https"))

        return frozenset(("data",))


# Restrict the usage page to its local logo and inline CSS/JavaScript.
# Disable forms, base-URL overrides, framing, and referrer disclosure.
INDEX_HEADERS: tuple[Header, ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; img-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime configuration loaded from environment variables."""

    # token authenticates render requests when non-empty.
    token: str
    # version identifies the running service build on the usage page.
    version: str
    # workers configures the Gunicorn worker count displayed to operators.
    workers: int
    # timeout configures the Gunicorn request timeout in seconds.
    timeout: int
    # max_html_bytes bounds the accepted UTF-8 request body size.
    max_html_bytes: int
    # max_pdf_bytes bounds the generated PDF response size.
    max_pdf_bytes: int
    # listen_address identifies the configured service bind address.
    listen_address: str = "0.0.0.0:8080"
    # asset_policy controls whether rendered documents may fetch remote assets.
    asset_policy: AssetPolicy = AssetPolicy.EMBEDDED
    # remote_asset_hosts is the exact host allowlist used in remote mode; "*" allows all hosts.
    remote_asset_hosts: frozenset[str] = frozenset()
    # allow_private_remote_assets permits approved hosts to resolve to non-public addresses.
    allow_private_remote_assets: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Load and validate runtime configuration."""
        token = cls._env_str(env, "HTML2PDF__TOKEN")
        allow_unauthenticated = cls._env_bool(env, "HTML2PDF__ALLOW_UNAUTHENTICATED", False)
        if not token and not allow_unauthenticated:
            raise ValueError("HTML2PDF__TOKEN is required unless HTML2PDF__ALLOW_UNAUTHENTICATED=true")

        asset_policy = cls._env_asset_policy(env)
        remote_asset_hosts = cls._env_remote_asset_hosts(env)
        if asset_policy is AssetPolicy.REMOTE and not remote_asset_hosts:
            raise ValueError("HTML2PDF__REMOTE_ASSET_HOSTS is required when HTML2PDF__ASSET_POLICY=remote")

        return cls(
            token=token,
            version=cls._env_str(env, "HTML2PDF__VERSION", "dev"),
            listen_address=cls._env_str(env, "HTML2PDF__LISTEN_ADDRESS", "0.0.0.0:8080"),
            workers=cls._env_int(env, "HTML2PDF__WORKERS", 2),
            timeout=cls._env_int(env, "HTML2PDF__TIMEOUT", 45),
            max_html_bytes=cls._env_int(env, "HTML2PDF__MAX_HTML_BYTES", 32 * 1024 * 1024),
            max_pdf_bytes=cls._env_int(env, "HTML2PDF__MAX_PDF_BYTES", 64 * 1024 * 1024),
            asset_policy=asset_policy,
            remote_asset_hosts=remote_asset_hosts,
            allow_private_remote_assets=cls._env_bool(
                env, "HTML2PDF__ALLOW_PRIVATE_REMOTE_ASSETS", False),
        )

    @staticmethod
    def _env_str(env: Mapping[str, str], name: str, default: str = "") -> str:
        """Read a stripped string, using the default for blank values."""
        return env.get(name, "").strip() or default

    @classmethod
    def _env_asset_policy(cls, env: Mapping[str, str]) -> AssetPolicy:
        """Read and validate the asset-fetching policy."""
        name = "HTML2PDF__ASSET_POLICY"
        value = cls._env_str(env, name, AssetPolicy.EMBEDDED.value).casefold()

        try:
            return AssetPolicy(value)
        except ValueError:
            allowed = ", ".join(policy.value for policy in AssetPolicy)
            raise ValueError(f"{name} must be one of: {allowed}") from None

    @classmethod
    def _env_remote_asset_hosts(cls, env: Mapping[str, str]) -> frozenset[str]:
        """Read a comma-separated remote asset host allowlist or explicit wildcard."""
        name = "HTML2PDF__REMOTE_ASSET_HOSTS"
        values = [value.strip().casefold() for value in env.get(name, "").split(",") if value.strip()]
        for value in values:
            parsed = urlsplit("//" + value)
            if parsed.hostname != value or parsed.port is not None or "/" in value:
                raise ValueError(f"{name} must contain hostnames only")
        return frozenset(values)

    @classmethod
    def _env_bool(cls, env: Mapping[str, str], name: str, default: bool) -> bool:
        """Read a strict boolean environment variable."""
        value = cls._env_str(env, name)
        if not value:
            return default
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
        raise ValueError(f"{name} must be true or false")

    @classmethod
    def _env_int(cls, env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
        """Read and validate an integer environment variable."""
        value = cls._env_str(env, name)
        if not value:
            return default

        try:
            result = int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None

        if result < minimum:
            raise ValueError(f"{name} must be at least {minimum}")

        return result


class AssetError(ValueError):
    """A document references an asset rejected by the configured policy."""


class RequestError(ValueError):
    """An HTTP request failed validation."""

    # status is the HTTP status returned for the invalid request.
    status: HTTPStatus
    # body is the safe response body describing the validation failure.
    body: bytes

    def __init__(self, status: HTTPStatus, body: bytes) -> None:
        """Initialize a client-safe validation failure response."""
        super().__init__(body.decode("utf-8", errors="replace").strip())
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class Request:
    """Small typed wrapper around the WSGI request environment."""

    # _environ contains the server-provided WSGI request values.
    _environ: Environ

    def _string(self, key: str) -> str:
        """Return a string value from the WSGI environment."""
        return cast(str, self._environ.get(key, ""))

    @property
    def method(self) -> str:
        """Return the HTTP request method."""
        return self._string("REQUEST_METHOD")

    @property
    def path(self) -> str:
        """Return the requested path."""
        return self._string("PATH_INFO")

    @property
    def content_type(self) -> str:
        """Return the raw Content-Type value."""
        return self._string("CONTENT_TYPE").strip()

    @property
    def media_type(self) -> str:
        """Return the normalized request media type."""
        return self.content_type.partition(";")[0].strip().casefold()

    @property
    def charset(self) -> str | None:
        """Return the normalized charset parameter when supplied."""
        # MIME parameter parsing preserves semicolons inside quoted values.
        message = Message()
        message["Content-Type"] = self.content_type
        value = message.get_param("charset")
        if value is None:
            return None
        # Extended parameter tuples are not supported charset declarations.
        return value.strip().casefold() if isinstance(value, str) else ""

    @property
    def content_length(self) -> str:
        """Return the raw Content-Length value."""
        return self._string("CONTENT_LENGTH").strip()

    @property
    def authorization(self) -> str:
        """Return the Authorization header."""
        return self._string("HTTP_AUTHORIZATION").strip()

    def read(self, length: int) -> bytes:
        """Read up to length bytes from the request body."""
        stream = cast(BinaryIO, self._environ["wsgi.input"])
        return stream.read(length)


def format_bytes(value: int) -> str:
    """Return a human-readable binary byte size."""
    units = ("B", "KiB", "MiB", "GiB")
    size = float(value)

    for unit in units:
        if size >= 1024 and unit != units[-1]:
            size /= 1024
            continue

        if size.is_integer():
            return f"{int(size)} {unit}"

        return f"{size:.1f} {unit}"

    raise AssertionError("unreachable")


def load_index(config: Config) -> bytes:
    """Load the usage page and substitute runtime information."""
    replacements = {
        "{{VERSION}}": config.version,
        "{{AUTH_STATUS}}": "enabled" if config.token else "disabled",
        "{{WORKERS}}": str(config.workers),
        "{{LISTEN_ADDRESS}}": config.listen_address,
        "{{TIMEOUT}}": str(config.timeout),
        "{{MAX_HTML_SIZE}}": format_bytes(config.max_html_bytes),
        "{{MAX_PDF_SIZE}}": format_bytes(config.max_pdf_bytes),
        "{{ASSET_POLICY}}": config.asset_policy.value,
    }

    page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    # Substitute only template text, never placeholders inside runtime values.
    page = re.sub(
        r"{{[A-Z_]+}}",
        lambda match: html.escape(replacements[match[0]], quote=True),
        page,
    )

    return page.encode("utf-8")


def render_document(
    source: str,
    asset_policy: AssetPolicy = AssetPolicy.EMBEDDED,
    remote_asset_hosts: frozenset[str] = frozenset(),
    allow_private_remote_assets: bool = False,
) -> bytes:
    """Render one HTML document using the configured asset-fetching policy."""

    class AssetFetcher(URLFetcher):
        """Reject asset schemes that are not enabled by the configured policy."""

        def __init__(self) -> None:
            """Initialize a fetcher that records policy violations."""
            # Redirects stay disabled so an allowed HTTP(S) URL cannot redirect
            # to a scheme outside the policy before it is validated here.
            super().__init__(allow_redirects=False)
            self.rejected = False

        def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
            """Fetch an allowed asset URL or reject it before network access."""
            scheme = urlsplit(url).scheme.casefold()

            if scheme not in asset_policy.allowed_schemes:
                self.rejected = True
                raise AssetError(
                    f"Asset URL scheme {scheme or '<none>'!r} is not allowed "
                    f"by policy {asset_policy.value!r}"
                )

            if scheme in ("http", "https"):
                try:
                    validate_remote_asset_url(url, remote_asset_hosts, allow_private_remote_assets)
                except AssetError:
                    # WeasyPrint may continue after an asset-fetch failure; retain
                    # this policy decision so the request still fails as a whole.
                    self.rejected = True
                    raise

            return super().fetch(url, headers)

    fetcher = AssetFetcher()
    result = HTML(
        string=source,
        # Resolve relative assets so they reach the policy fetcher rather than
        # being silently discarded as unresolved references.
        base_url="https://html2pdf.invalid/",
        url_fetcher=fetcher,
    ).write_pdf()

    # WeasyPrint can log asset failures and continue. Turn policy rejections
    # into a hard error so callers never receive a silently incomplete PDF.
    if fetcher.rejected:
        raise AssetError(f"Asset rejected by {asset_policy.value!r} policy")

    return result


def validate_remote_asset_url(
    value: str, allowed_hosts: frozenset[str], allow_private_addresses: bool,
) -> None:
    """Require an allowed remote hostname and reject unsafe resolved addresses."""
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None or "*" not in allowed_hosts and hostname.casefold() not in allowed_hosts:
        raise AssetError("Remote asset host is not allowed")

    if allow_private_addresses:
        return

    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AssetError("Remote asset host could not be resolved") from exc

    for _, _, _, _, address in addresses:
        if not ipaddress.ip_address(address[0]).is_global:
            raise AssetError("Remote asset host resolved to a non-public address")


def bearer_token(request: Request) -> str:
    """Return the submitted bearer token, or an empty string when absent."""
    authorization = request.authorization
    if not authorization:
        return ""

    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        return ""

    return parts[1].strip()


def authorized(request: Request, config: Config) -> bool:
    """Report whether the request may use the render endpoint."""
    if not config.token:
        return True

    submitted = bearer_token(request)

    return hmac.compare_digest(submitted.encode("utf-8"), config.token.encode("utf-8"))


def duration_ms(started: float) -> int:
    """Return elapsed milliseconds since a perf-counter timestamp."""
    return round((time.perf_counter() - started) * 1000)


def respond(
    start_response: StartResponse,
    status: HTTPStatus,
    body: bytes,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: Iterable[Header] = (),
) -> Response:
    """Build a WSGI response."""
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        *extra_headers,
    ]

    start_response(f"{status.value} {status.phrase}", headers)

    return [body]


def method_not_allowed(start_response: StartResponse, allowed: str) -> Response:
    """Return a method-not-allowed response for a known endpoint."""
    return respond(
        start_response,
        HTTPStatus.METHOD_NOT_ALLOWED,
        f"Use {allowed}\n".encode(),
        extra_headers=[("Allow", allowed)],
    )


def read_html(request: Request, config: Config) -> tuple[str, int]:
    """Validate and read a UTF-8 HTML request body."""
    if request.media_type != "text/html":
        raise RequestError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            b"Send text/html encoded as UTF-8\n",
        )

    if request.charset not in (None, "utf-8", "utf8"):
        raise RequestError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            b"Send text/html encoded as UTF-8\n",
        )

    content_length = request.content_length

    if not content_length:
        raise RequestError(
            HTTPStatus.LENGTH_REQUIRED,
            b"Content-Length is required\n",
        )

    if not content_length.isascii() or not content_length.isdecimal():
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            b"Invalid Content-Length\n",
        )

    normalized_length = content_length.lstrip("0") or "0"

    if normalized_length == "0":
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            b"HTML is required\n",
        )

    # Reject absurdly large values before converting attacker-controlled input.
    max_length = str(config.max_html_bytes)
    if len(normalized_length) > len(max_length):
        raise RequestError(
            HTTPStatus.CONTENT_TOO_LARGE,
            b"HTML exceeds configured size limit\n",
        )

    length = int(normalized_length)

    if length > config.max_html_bytes:
        raise RequestError(
            HTTPStatus.CONTENT_TOO_LARGE,
            b"HTML exceeds configured size limit\n",
        )

    body = request.read(length)

    if len(body) != length:
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            b"Incomplete request body\n",
        )

    try:
        source = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            b"HTML must be UTF-8\n",
        ) from exc

    return source, length


def render_response(
    request: Request,
    start_response: StartResponse,
    config: Config,
) -> Response:
    """Validate a render request, render its PDF, and return the response."""
    if not authorized(request, config):
        return respond(
            start_response,
            HTTPStatus.UNAUTHORIZED,
            b"Invalid or missing bearer token\n",
            extra_headers=[("WWW-Authenticate", "Bearer")],
        )

    try:
        source, html_bytes = read_html(request, config)
    except RequestError as exc:
        return respond(
            start_response,
            exc.status,
            exc.body,
        )

    started = time.perf_counter()

    try:
        if config.asset_policy is AssetPolicy.REMOTE:
            result = render_document(
                source,
                config.asset_policy,
                config.remote_asset_hosts,
                config.allow_private_remote_assets,
            )
        else:
            result = render_document(source, config.asset_policy)
    except AssetError:
        logger.warning(
            "render rejected duration_ms=%d html_bytes=%d reason=asset_policy",
            duration_ms(started),
            html_bytes,
        )

        if config.asset_policy is AssetPolicy.REMOTE:
            message = b"Only allowlisted remote HTTP(S) asset hosts are allowed\n"
        else:
            message = (
                b"Embed images, fonts, and other assets as data URLs; "
                b"external assets are not fetched\n"
            )

        return respond(
            start_response,
            HTTPStatus.UNPROCESSABLE_CONTENT,
            message,
        )
    except Exception:
        logger.exception(
            "render failed duration_ms=%d html_bytes=%d reason=internal_error",
            duration_ms(started),
            html_bytes,
        )

        return respond(
            start_response,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            b"PDF rendering failed\n",
        )

    pdf_bytes = len(result)

    if pdf_bytes > config.max_pdf_bytes:
        logger.warning(
            "render rejected duration_ms=%d html_bytes=%d pdf_bytes=%d reason=pdf_too_large",
            duration_ms(started),
            html_bytes,
            pdf_bytes,
        )

        return respond(
            start_response,
            HTTPStatus.CONTENT_TOO_LARGE,
            b"PDF exceeds configured size limit\n",
        )

    logger.info(
        "render completed duration_ms=%d html_bytes=%d pdf_bytes=%d",
        duration_ms(started),
        html_bytes,
        pdf_bytes,
    )

    return respond(
        start_response,
        HTTPStatus.OK,
        result,
        "application/pdf",
    )


def create_application(config: Config) -> Application:
    """Create a WSGI application with its own configuration and cached assets."""
    index_html = load_index(config)
    logo_svg = Path(__file__).with_name("logo.svg").read_bytes()

    def application(environ: Environ, start_response: StartResponse) -> Response:
        """Serve the index, logo, health check, and HTML-to-PDF endpoint."""
        request = Request(environ)

        match request.path:
            case "/":
                if request.method != "GET":
                    return method_not_allowed(start_response, "GET")

                return respond(
                    start_response,
                    HTTPStatus.OK,
                    index_html,
                    "text/html; charset=utf-8",
                    INDEX_HEADERS,
                )

            case "/logo.svg":
                if request.method != "GET":
                    return method_not_allowed(start_response, "GET")

                return respond(
                    start_response,
                    HTTPStatus.OK,
                    logo_svg,
                    "image/svg+xml",
                )

            case "/healthz":
                if request.method != "GET":
                    return method_not_allowed(start_response, "GET")

                return respond(
                    start_response,
                    HTTPStatus.OK,
                    b"ok\n",
                )

            case "/render":
                if request.method != "POST":
                    return method_not_allowed(start_response, "POST")

                return render_response(
                    request,
                    start_response,
                    config,
                )

            case _:
                return respond(
                    start_response,
                    HTTPStatus.NOT_FOUND,
                    b"Not found\n",
                )

    return application


application = create_application(Config.from_env(os.environ))
