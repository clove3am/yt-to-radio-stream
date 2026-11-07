"""Small shared helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CREDENTIALS_RE = re.compile(r"(://[^/\s:@]+:)[^@\s]+@")

YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}


class InvalidYouTubeURL(ValueError):
    """Raised when a URL is not a usable YouTube video/live URL."""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def redact_secrets(text: str) -> str:
    """Remove userinfo credentials from URLs before logging them."""

    return _CREDENTIALS_RE.sub(r"\1***@", text)


def normalize_youtube_url(raw: str) -> str:
    """Validate and normalise a YouTube URL.

    Accepts any public youtube.com subdomain or youtu.be link. This is a
    server-side fetch, so the host allow-list is intentional.
    """

    value = (raw or "").strip()
    if not value:
        raise InvalidYouTubeURL("Enter a YouTube video or live stream URL.")

    if "://" not in value:
        value = "https://" + value

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidYouTubeURL("The URL must start with http:// or https://.")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidYouTubeURL("That URL does not include a hostname.")

    allowed = host in YOUTUBE_HOSTS or host.endswith(".youtube.com")
    if not allowed:
        raise InvalidYouTubeURL(
            "Only YouTube links are supported (youtube.com or youtu.be)."
        )

    if parsed.path in {"", "/"}:
        raise InvalidYouTubeURL("Enter a link to a specific YouTube live stream.")

    # Drop fragments; they are never needed by yt-dlp.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def slugify(value: str, fallback: str = "station") -> str:
    normalised = unicodedata.normalize("NFKD", value)
    ascii_value = normalised.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_value.lower()).strip("-")
    slug = slug[:48].strip("-")
    return slug or fallback


def truncate(text: str, limit: int = 500) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
