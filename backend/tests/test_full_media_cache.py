"""Full-size media is browser-cacheable and answers a revalidation with 304.

It went out with an ETag but no Cache-Control, and `If-None-Match: <that etag>` re-sent the whole file.
"""

import os

from starlette.requests import Request
from starlette.responses import FileResponse

from backend import main


def _request(**headers):
    return Request(
        {"type": "http", "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]},
    )


def _serve(tmp_path, **headers):
    f = tmp_path / "img.png"
    if not f.exists():
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    kwargs = main._media_response_kwargs(
        "image/png",
        f.read_bytes()[:16],
        str(f),
        **{"Cache-Control": main._FULL_MEDIA_CACHE_CONTROL},
    )
    return main._full_media_file_response(_request(**headers), str(f), **kwargs)


def test_a_first_request_gets_the_file_with_cache_control_and_validators(tmp_path):
    r = _serve(tmp_path)
    assert isinstance(r, FileResponse) and r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["etag"] and r.headers["last-modified"]


def test_a_matching_if_none_match_is_a_304_without_a_body(tmp_path):
    etag = _serve(tmp_path).headers["etag"]
    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        r = _serve(tmp_path, if_none_match=header)
        assert r.status_code == 304, header
        assert r.body == b"" and r.headers["etag"] == etag and "cache-control" in r.headers


def test_a_changed_file_is_sent_again(tmp_path):
    etag = _serve(tmp_path).headers["etag"]
    f = tmp_path / "img.png"
    f.write_bytes(f.read_bytes() + b"more")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 5))
    assert _serve(tmp_path, if_none_match=etag).status_code == 200


def test_if_modified_since_only_counts_without_if_none_match(tmp_path):
    last = _serve(tmp_path).headers["last-modified"]
    assert _serve(tmp_path, if_modified_since=last).status_code == 304
    assert _serve(tmp_path, if_modified_since="Mon, 01 Jan 2001 00:00:00 GMT").status_code == 200
    assert _serve(tmp_path, if_none_match='"stale"', if_modified_since=last).status_code == 200
    assert _serve(tmp_path, if_modified_since="not a date").status_code == 200
