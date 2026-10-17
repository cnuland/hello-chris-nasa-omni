"""Image sniffing for operator uploads (content-type AND magic bytes must agree on 'image')."""

from __future__ import annotations

# (magic prefix check, canonical content-type, file extension)
_SIGNATURES: list[tuple[str, str, str]] = [
    ("png", "image/png", "png"),
    ("jpeg", "image/jpeg", "jpg"),
    ("gif", "image/gif", "gif"),
    ("webp", "image/webp", "webp"),
    ("tiff", "image/tiff", "tif"),
    ("bmp", "image/bmp", "bmp"),
]


def _matches(kind: str, data: bytes) -> bool:
    if kind == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if kind == "gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if kind == "webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if kind == "tiff":
        return data.startswith((b"II*\x00", b"MM\x00*"))
    if kind == "bmp":
        return len(data) >= 26 and data.startswith(b"BM")
    return False


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """Return (content_type, extension) for a recognised image, else None."""
    for kind, content_type, ext in _SIGNATURES:
        if _matches(kind, data):
            return content_type, ext
    return None
