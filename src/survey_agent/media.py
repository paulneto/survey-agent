"""Tiny media helpers for rendering video stimulus. Pure string utilities (no deps); used by the
runtime frontends. Video here is *stimulus* (show a YouTube embed or a video URL) — not a response
type, so the engine never touches media bytes."""
import re

_YT = [
    re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([A-Za-z0-9_-]{6,})"),
]


def youtube_id(url):
    """Extract a YouTube video id from common URL shapes, or None."""
    for rx in _YT:
        m = rx.search(url or "")
        if m:
            return m.group(1)
    return None


def youtube_embed(url):
    """Return a privacy-friendly nocookie embed URL for a YouTube link, or None if not YouTube."""
    vid = youtube_id(url)
    return f"https://www.youtube-nocookie.com/embed/{vid}" if vid else None
