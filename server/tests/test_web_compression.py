import gzip
import tracemalloc
import zlib

import httpx
import pytest

from radar.web_reader import PageFetcher, PageUnavailable


class WireStream(httpx.AsyncByteStream):
    def __init__(self, data, chunk_size=17):
        self.data, self.chunk_size = data, chunk_size
        self.reads, self.closed = 0, False

    async def __aiter__(self):
        for offset in range(0, len(self.data), self.chunk_size):
            self.reads += 1
            yield self.data[offset:offset + self.chunk_size]

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def addresses(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr("radar.web_reader.public_addresses", addresses)


def route_response(respx_mock, body, encoding="gzip", *, status=200, path="/article", chunk_size=17):
    stream = WireStream(body, chunk_size)
    route = respx_mock.get("https://93.184.216.34" + path).mock(return_value=httpx.Response(
        status, headers={"content-encoding": encoding, "content-type": "text/plain"}, stream=stream))
    return route, stream


@pytest.mark.parametrize("encoding,compress", [
    ("gzip", gzip.compress),
    (" GZip ", gzip.compress),
    ("x-gzip", gzip.compress),
    ("deflate", zlib.compress),
    ("deflate", lambda data: zlib.compress(data, wbits=-zlib.MAX_WBITS)),
    ("identity", bytes),
])
async def test_public_compressed_page_is_read_without_login(respx_mock, encoding, compress):
    body = ("公开研究网页 / AI model evaluation\n" * 40).encode()
    route, stream = route_response(respx_mock, compress(body), encoding)
    url, status, headers, actual = await PageFetcher().bytes("https://safe.example/article", check_robots=False)
    assert actual == body and status == 200 and url == "https://safe.example/article"
    assert headers["content-type"] == "text/plain"
    request = route.calls[0].request
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["host"] == "safe.example"
    assert request.extensions["sni_hostname"] == "safe.example"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert stream.closed


async def test_concatenated_gzip_members_preserve_content(respx_mock):
    body = b"first article section " * 10 + b"second article section " * 10
    route_response(respx_mock, gzip.compress(b"first article section " * 10)
                   + gzip.compress(b"second article section " * 10))
    result = await PageFetcher().bytes("https://safe.example/article", check_robots=False)
    assert result[3] == body


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_output_limit_prevents_expansion_bomb(respx_mock, encoding, compress):
    wire = compress(b"x" * 10_000_000)
    limit = 32_768
    assert len(wire) < limit
    _, stream = route_response(respx_mock, wire, encoding, chunk_size=65536)
    tracemalloc.start()
    try:
        with pytest.raises(PageUnavailable) as error:
            await PageFetcher().bytes("https://safe.example/article", limit=limit, check_robots=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.status == "too_large"
    # HTTPX plus decoding should not allocate the bomb's 10 MB output first.
    assert peak < 2_000_000
    assert stream.closed


async def test_compressed_input_limit_is_independent_of_output(respx_mock):
    # Many empty gzip members have no decoded text but still consume wire bytes.
    wire = gzip.compress(b"") * 100
    _, stream = route_response(respx_mock, wire)
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes("https://safe.example/article", limit=512, check_robots=False)
    assert error.value.status == "too_large" and "传输" in error.value.message
    assert stream.closed


@pytest.mark.parametrize("encoding,wire", [
    ("gzip", b"not a gzip stream"),
    ("gzip", b""),
    ("gzip", gzip.compress(b"research article " * 20)[:-4]),
    ("gzip", gzip.compress(b"research article " * 20)[:-8] + b"\0" * 8),
    ("deflate", b"not a deflate stream"),
    ("deflate", zlib.compress(b"research article " * 20)[:-2]),
    ("deflate", zlib.compress(b"research article " * 20) + b"trailing garbage"),
])
async def test_malformed_compression_is_not_reported_as_authorization(respx_mock, encoding, wire):
    _, stream = route_response(respx_mock, wire, encoding)
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes("https://safe.example/article", check_robots=False)
    assert error.value.status == "unavailable" and "压缩" in error.value.message
    assert "登录" not in error.value.message and stream.closed


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate"])
async def test_unsupported_encoding_stops_before_reading(respx_mock, encoding):
    _, stream = route_response(respx_mock, b"unsupported encoded bytes", encoding)
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes("https://safe.example/article", check_robots=False)
    assert error.value.status == "unsupported" and "压缩" in error.value.message
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_decoded_content_exactly_at_limit_is_accepted(respx_mock, encoding, compress):
    body = b"article text " * 40
    route_response(respx_mock, compress(body), encoding)
    result = await PageFetcher().bytes("https://safe.example/article", limit=len(body), check_robots=False)
    assert result[3] == body


@pytest.mark.parametrize("status,expected", [(401, "auth_required"), (403, "access_restricted")])
async def test_http_challenge_is_not_decoded_or_asserted_to_require_account(respx_mock, status, expected):
    _, stream = route_response(respx_mock, b"not gzip", status=status)
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes("https://safe.example/article", check_robots=False)
    assert error.value.status == expected and "不一定需要账号登录" in error.value.message
    assert stream.reads == 0 and stream.closed


async def test_compressed_robots_still_blocks_disallowed_article(respx_mock):
    _, stream = route_response(respx_mock, gzip.compress(b"User-agent: *\nDisallow: /article\n"),
                               path="/robots.txt")
    with pytest.raises(PageUnavailable) as error:
        await PageFetcher().bytes("https://safe.example/article")
    assert error.value.status == "restricted"
    assert len(respx_mock.calls) == 1 and stream.closed
