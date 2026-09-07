"""Bounded public-page reads. DNS is validated and pinned on every redirect hop."""
import asyncio
import gzip
import io
import ipaddress
import socket
import zlib
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .links import normalize_link

USER_AGENT = "AIRadar/0.3 (personal source reader)"


class PageUnavailable(Exception):
    def __init__(self, status: str, message: str):
        self.status, self.message = status, message
        super().__init__(message)


async def response_body(response: httpx.Response, limit: int) -> bytes:
    """Bound both wire bytes and decoded bytes, including servers ignoring identity.

    HTTPX's aiter_bytes decodes before applying its chunk size, which is not an
    expansion limit. Read raw bytes and use the standard library's bounded APIs.
    """
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
        raise PageUnavailable("unsupported", "页面使用暂不支持的压缩编码，未读取正文。")
    raw = bytearray()
    async for chunk in response.aiter_raw(chunk_size=65536):
        if len(raw) + len(chunk) > limit:
            raise PageUnavailable("too_large", "页面传输内容超过单次读取大小限制。")
        raw.extend(chunk)
    if encoding in {"", "identity"}:
        return bytes(raw)
    try:
        if encoding in {"gzip", "x-gzip"}:
            if not raw:
                raise EOFError("empty gzip response")
            # read(size) caps expansion and checks gzip members/checksums on EOF.
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                data = stream.read(limit + 1)
        else:
            # Some HTTP servers send raw DEFLATE instead of its standard zlib
            # wrapper. Choose from the two-byte header, never retry a bad checksum.
            wrapped = len(raw) >= 2 and raw[0] & 15 == 8 and (raw[0] * 256 + raw[1]) % 31 == 0
            decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            data = decoder.decompress(raw, limit + 1)
            if len(data) <= limit and (not decoder.eof or decoder.unused_data):
                raise ValueError("incomplete or trailing deflate stream")
    except (OSError, EOFError, zlib.error, ValueError):
        raise PageUnavailable("unavailable", "页面压缩内容不完整或损坏，稍后自动重试。") from None
    if len(data) > limit:
        raise PageUnavailable("too_large", "页面解压后的正文超过单次读取大小限制。")
    return data


def public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            return False
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped and not public_ip(str(ip.ipv4_mapped)):
                return False
            if ip.sixtofour or ip.teredo:
                return False
        return True
    except ValueError:
        return False


async def public_addresses(host: str, port: int) -> list[str]:
    if "%" in host or host.lower().endswith((".localhost", ".local", ".internal")) or host == "localhost":
        raise PageUnavailable("blocked", "该链接不是可读取的公开网页。")
    try:
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM), timeout=5)
    except (OSError, TimeoutError) as exc:
        raise PageUnavailable("unavailable", "页面域名暂时无法解析，稍后重试。") from exc
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or not all(public_ip(ip) for ip in ips):
        raise PageUnavailable("blocked", "该链接不是可读取的公开网页。")
    return sorted(ips, key=lambda ip: ":" in ip)


class PageFetcher:
    def __init__(self):
        self.robots: dict[str, RobotFileParser] = {}
        self.robots_errors: dict[str, tuple[str, str]] = {}
        self.robots_locks: dict[str, asyncio.Lock] = {}

    async def bytes(self, original: str, *, headers=None, limit=8_000_000, check_robots=True):
        async with asyncio.timeout(55):
            url = original
            for _ in range(6):
                normalized = normalize_link(url)
                if not normalized:
                    raise PageUnavailable("blocked", "链接格式不受支持。")
                url = normalized
                if check_robots:
                    await self.check_robots(url)
                target = httpx.URL(url)
                host = target.host
                addresses = await public_addresses(host, target.port or (443 if target.scheme == "https" else 80))
                # Connect to the validated address, never resolve the hostname a second time.
                pinned = target.copy_with(host=addresses[0])
                request_headers = {"User-Agent": USER_AGENT, "Host": target.netloc.decode(),
                                   "Accept-Encoding": "identity", **(headers or {})}
                # A new client per hop prevents cookies and pooled TLS connections crossing origins.
                async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                    async with client.stream("GET", pinned, headers=request_headers,
                                             extensions={"sni_hostname": host}) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise PageUnavailable("unavailable", "页面重定向缺少目标地址。")
                            url = urljoin(url, location)
                            headers = None  # No conditional headers carried to another destination.
                            continue
                        if response.status_code == 304:
                            return url, response.status_code, dict(response.headers), b""
                        if response.status_code == 401:
                            raise PageUnavailable("auth_required", "网站要求登录或访问验证，可打开浏览器检查；不一定需要账号登录。")
                        if response.status_code == 403:
                            raise PageUnavailable("access_restricted", "网站拒绝自动访问，可能需要浏览器验证；不一定需要账号登录。")
                        if response.status_code == 429:
                            raise PageUnavailable("rate_limited", "页面访问频率受限，稍后重试。")
                        response.raise_for_status()
                        data = await response_body(response, limit)
                        return url, response.status_code, dict(response.headers), data
            raise PageUnavailable("unavailable", "链接重定向过多，尚未取得正文。")

    async def check_robots(self, url: str):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        # One lookup per origin per collection, even when concurrent articles hit
        # a challenge or timeout. A new PageFetcher next run retries failed rules.
        async with self.robots_locks.setdefault(origin, asyncio.Lock()):
            if origin in self.robots_errors:
                raise PageUnavailable(*self.robots_errors[origin])
            if origin not in self.robots:
                parser = RobotFileParser(origin + "/robots.txt")
                try:
                    try:
                        _, _, _, data = await self.bytes(parser.url, limit=150000, check_robots=False)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code not in {404, 410}:
                            raise
                        data = b""
                    parser.parse(data.decode("utf-8", errors="replace").splitlines())
                except (PageUnavailable, httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
                    failure = ((exc.status, exc.message) if isinstance(exc, PageUnavailable) else
                               ("unavailable", "暂时无法确认页面的自动读取规则，稍后重试。"))
                    self.robots_errors[origin] = failure
                    raise PageUnavailable(*failure) from None
                self.robots[origin] = parser
        if not self.robots[origin].can_fetch("AIRadar", url):
            raise PageUnavailable("restricted", "网站不允许自动读取该页面，未抓取正文。")
