"""Reverse-image-search guards: the /search/by-image path must refuse what it can't
answer honestly — a field with no server-side image tower (DINO etc.), a query image
whose encoded dimension doesn't match the field, a missing image source, an unreadable
image, or an SSRF-y URL — and must plumb the encoded vector into the exact same search
the vector= path uses.

The image encoder itself is exercised through a stub (transformers isn't a backend dep
and CI doesn't install it — the 501 contract covers that); has_image_tower is pure and
tested directly. Self-contained, matching the suite.
"""

import asyncio
import base64

import httpx
import pytest
from fastapi import HTTPException

from backend import encoders
from backend.models import ImageSearchRequest
from backend.routers import search as search_router


def test_image_tower_families():
    assert encoders.has_image_tower(None)  # unregistered → assume CLIP (pre-registry datasets)
    assert encoders.has_image_tower("openai/clip-vit-base-patch32")
    assert not encoders.has_image_tower("facebook/dinov2-base")
    assert not encoders.has_image_tower("dinov3")


class _DB:
    """Stubs search.db: fetchval answers the registry probe then the dim probe."""

    def __init__(self, registered=None, dim=512):
        self._answers = {"embedding_field_models": registered, "vector_dims": dim}

    async def fetchval(self, query, *args):
        for key, val in self._answers.items():
            if key in query:
                return val
        return None


def _b64(data: bytes = b"not-a-real-image-but-encode-is-stubbed") -> str:
    return base64.b64encode(data).decode()


def _search(req, db, monkeypatch, encoded=None, capture=None):
    """Run search_by_image with db/encoder/auth/vector_search stubbed. vector_search is
    replaced by a capture so the test asserts the endpoint reaches it with the encoded
    vector rather than re-testing the KNN itself."""
    monkeypatch.setattr(search_router, "db", db)

    async def fake_encode(image_bytes, registered_model=None):
        return encoded if encoded is not None else [0.1] * 512

    monkeypatch.setattr(search_router, "encode_image", fake_encode)

    async def fake_vector_search(search_req, request):
        if capture is not None:
            capture["req"] = search_req
        return [{"id": 7, "distance": 0.0}]

    monkeypatch.setattr(search_router, "vector_search", fake_vector_search)

    async def fake_user(request):
        return {"id": 1, "role": "admin"}

    async def fake_access(dataset_id, user):
        return None

    monkeypatch.setattr(search_router, "get_optional_user", fake_user)
    monkeypatch.setattr(search_router, "check_dataset_access", fake_access)

    class _Req:
        headers: dict = {}
        cookies: dict = {}

    return asyncio.run(search_router.search_by_image(req, _Req()))  # type: ignore[arg-type]


def test_encoded_vector_flows_into_vector_search(monkeypatch):
    capture: dict = {}
    req = ImageSearchRequest(dataset_id=1, image_base64=_b64(), field_name="clip", limit=5)
    out = _search(req, _DB(registered=None, dim=512), monkeypatch, encoded=[0.2] * 512, capture=capture)
    assert out == [{"id": 7, "distance": 0.0}]
    # The endpoint must hand vector_search the ENCODED image vector under the same field.
    assert capture["req"].vector == [0.2] * 512
    assert capture["req"].field_name == "clip"
    assert capture["req"].dataset_id == 1
    assert capture["req"].limit == 5


def test_image_against_towerless_field_refused(monkeypatch):
    req = ImageSearchRequest(dataset_id=1, image_base64=_b64(), field_name="dinov3")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(registered="facebook/dinov2-base"), monkeypatch)
    assert ei.value.status_code == 400
    assert "image encoder" in ei.value.detail


def test_dim_mismatch_refused(monkeypatch):
    req = ImageSearchRequest(dataset_id=1, image_base64=_b64(), field_name="bigclip")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(registered=None, dim=1024), monkeypatch, encoded=[0.1] * 512)
    assert ei.value.status_code == 400
    assert "1024" in ei.value.detail and "512" in ei.value.detail


def test_missing_image_source_refused(monkeypatch):
    req = ImageSearchRequest(dataset_id=1, field_name="clip")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(), monkeypatch)
    assert ei.value.status_code == 400
    assert "image_base64 or image_url" in ei.value.detail


def test_bad_base64_refused(monkeypatch):
    req = ImageSearchRequest(dataset_id=1, image_base64="!!!not base64!!!", field_name="clip")
    with pytest.raises(HTTPException) as ei:
        _search(req, _DB(), monkeypatch)
    assert ei.value.status_code == 400
    assert "base64" in ei.value.detail


def test_data_url_prefix_is_stripped():
    # The `data:image/...;base64,` prefix a browser FileReader emits must be tolerated.
    raw = b"abcdef"
    decoded = search_router._decode_base64_image("data:image/jpeg;base64," + base64.b64encode(raw).decode())
    assert decoded == raw


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x.png", "gopher://x"])
def test_ssrf_non_http_scheme_refused(url):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(search_router._fetch_image_from_url(url))
    assert ei.value.status_code == 400
    assert "http(s)" in ei.value.detail


def test_ssrf_loopback_refused():
    # localhost resolves to a loopback address — must be blocked before any fetch.
    with pytest.raises(HTTPException) as ei:
        asyncio.run(search_router._fetch_image_from_url("http://localhost/x.png"))
    assert ei.value.status_code == 400
    assert "internal" in ei.value.detail


# ---- regression: the guard must be `is_global`, and the fetch must be pinned ----


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",  # carrier-grade NAT — the Tailscale range the old flag list admitted
        "100.100.100.100",
        "100.127.255.254",
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "198.18.0.1",  # benchmarking
        "224.0.0.1",  # multicast
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",  # IPv6 ULA
        "::ffff:10.0.0.1",  # IPv4-mapped private
        "::ffff:100.64.0.1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_refused(address):
    assert search_router._address_is_public(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2001:4860:4860::8888", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address):
    assert search_router._address_is_public(address) is True


def test_shared_address_host_is_refused_before_any_fetch(monkeypatch):
    async def fake_resolve(host, port):
        # what the real resolver returns for a private-network hostname
        raise HTTPException(status_code=400, detail="image_url resolves to a disallowed (internal) address.")

    async def resolve_via_policy(host, port):
        # exercise the real policy with a canned DNS answer
        addresses = ["100.100.100.100"]
        if not all(search_router._address_is_public(a) for a in addresses):
            raise HTTPException(status_code=400, detail="image_url resolves to a disallowed (internal) address.")
        return addresses

    for resolver in (fake_resolve, resolve_via_policy):
        monkeypatch.setattr(search_router, "_resolve_public_addresses", resolver)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(search_router._fetch_image_from_url("http://private-host.example.test/x.png"))
        assert ei.value.status_code == 400 and "internal" in ei.value.detail


class _FakeResp:
    def __init__(self, status=200, ctype="image/jpeg", body=b"\xff\xd8\xff"):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, capture, resp, method, url, **kw):
        capture.update({"method": method, "url": url, **kw})
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_httpx(monkeypatch, capture, resp=None):
    resp = resp or _FakeResp()

    class _Client:
        def __init__(self, **kw):
            capture["client_kwargs"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kw):
            return _FakeStream(capture, resp, method, url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_fetch_is_pinned_to_the_validated_address_with_host_and_sni(monkeypatch):
    async def resolved(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search_router, "_resolve_public_addresses", resolved)
    capture: dict = {}
    _fake_httpx(monkeypatch, capture)
    out = asyncio.run(search_router._fetch_image_from_url("https://example.com:8443/pic.jpg?x=1"))
    assert out == b"\xff\xd8\xff"
    assert capture["url"] == "https://93.184.216.34:8443/pic.jpg?x=1"  # connect to the checked IP...
    assert capture["headers"]["Host"] == "example.com:8443"  # ...but speak to the named host
    assert capture["extensions"] == {"sni_hostname": "example.com"}  # TLS name check stays on the host
    assert capture["client_kwargs"]["follow_redirects"] is False


def test_http_fetch_pins_without_sni(monkeypatch):
    async def resolved(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search_router, "_resolve_public_addresses", resolved)
    capture: dict = {}
    _fake_httpx(monkeypatch, capture)
    asyncio.run(search_router._fetch_image_from_url("http://example.com/pic.jpg"))
    assert capture["url"] == "http://93.184.216.34/pic.jpg"
    assert capture["headers"]["Host"] == "example.com"
    assert capture["extensions"] == {}


@pytest.mark.parametrize("resp", [_FakeResp(status=404), _FakeResp(ctype="text/html")])
def test_upstream_status_and_type_are_not_an_oracle(monkeypatch, resp):
    async def resolved(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search_router, "_resolve_public_addresses", resolved)
    _fake_httpx(monkeypatch, {}, resp)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(search_router._fetch_image_from_url("https://example.com/x"))
    assert ei.value.status_code == 400
    assert "404" not in ei.value.detail and "text/html" not in ei.value.detail


def test_tiny_query_image_is_encoded_channels_last(monkeypatch):
    """A 1-pixel-high query image must not crash the server-side encoder (channel axis guess)."""
    torch = pytest.importorskip('torch')
    pytest.importorskip('transformers')
    import io
    from types import SimpleNamespace

    from PIL import Image

    seen = {}

    class FakeProcessor:
        def __call__(self, images, return_tensors, **kwargs):
            seen.update(kwargs)
            return {'pixel_values': torch.zeros(1, 3, 224, 224)}

    class FakeModel:
        def __call__(self, **inputs):
            return SimpleNamespace(image_embeds=torch.ones(1, 4))

    monkeypatch.setitem(encoders._image_cache, 'fixture-checkpoint', (FakeProcessor(), FakeModel()))
    buffer = io.BytesIO()
    Image.new('RGB', (81, 1)).save(buffer, 'PNG')
    assert encoders._encode_image_sync(buffer.getvalue(), 'fixture-checkpoint') == [1.0] * 4
    assert seen['input_data_format'] == 'channels_last'
