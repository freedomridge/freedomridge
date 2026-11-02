"""Media responses may never execute as a document on the app origin.

/api/samples/{id}/media served bytes under a type taken from the filename or the S3 object's
ContentType, so an `.svg` / `.html` "sample" ingested by any power_user was stored XSS with the
viewer's session when opened directly. The policy (backend/main.py _media_response_kwargs):
known raster/video types inline; otherwise the magic bytes decide (S3 objects uploaded without
a Content-Type must keep rendering); anything else is an opaque download; always nosniff + a
sandbox CSP. This pins the policy.
"""

import pytest

from backend import main as main_mod

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00"
SVG = b"<svg xmlns=\"http://www.w3.org/2000/svg\"><script>alert(1)</script></svg>"
HTML = b"<!doctype html><script>fetch('/api/users')</script>"


@pytest.mark.parametrize(
    "declared,head,expected",
    [
        ("image/jpeg", JPEG, "image/jpeg"),
        ("image/png; charset=binary", PNG, "image/png"),
        ("video/mp4", MP4, "video/mp4"),
        # S3 objects uploaded without a Content-Type: the bytes decide, and they keep rendering
        ("binary/octet-stream", JPEG, "image/jpeg"),
        ("application/octet-stream", PNG, "image/png"),
        (None, WEBP, "image/webp"),
        (None, MP4, "video/mp4"),
        # a lying declared type on real image bytes is still served as the image it is
        ("text/html", JPEG, "image/jpeg"),
    ],
)
def test_known_image_and_video_types_are_served_inline(declared, head, expected):
    kw = main_mod._media_response_kwargs(declared, head, "x.bin")
    assert kw["media_type"] == expected
    assert "Content-Disposition" not in kw["headers"]


@pytest.mark.parametrize(
    "declared,head,name",
    [
        ("image/svg+xml", SVG, "evil.svg"),
        ("text/html", HTML, "evil.html"),
        ("application/xhtml+xml", HTML, "evil.xhtml"),
        ("text/plain", b"hello world 12345", "notes.txt"),
        (None, SVG, "evil.svg"),
        ("application/octet-stream", HTML, "blob"),
    ],
)
def test_scriptable_or_unknown_content_is_an_opaque_download(declared, head, name):
    kw = main_mod._media_response_kwargs(declared, head, name)
    assert kw["media_type"] == "application/octet-stream"
    assert kw["headers"]["Content-Disposition"].startswith("attachment; filename=")


def test_svg_is_never_inline_even_though_it_is_an_image_type():
    assert "image/svg+xml" not in main_mod._INLINE_MEDIA_TYPES
    assert main_mod._inline_media_type("image/svg+xml", SVG) is None


def test_every_media_response_carries_nosniff_and_a_sandbox_csp():
    for declared, head in (("image/jpeg", JPEG), ("image/svg+xml", SVG)):
        h = main_mod._media_response_kwargs(declared, head, "f")["headers"]
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Content-Security-Policy"] == "sandbox"


def test_extra_headers_are_kept_and_download_filename_is_sanitised():
    kw = main_mod._media_response_kwargs(None, HTML, 'dir/we"ird name\n.html', **{"X-Cache": "HIT"})
    assert kw["headers"]["X-Cache"] == "HIT"
    assert kw["headers"]["Content-Disposition"] == 'attachment; filename="we_ird_name_.html"'
