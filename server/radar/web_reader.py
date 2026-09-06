"""Bounded public-page reads. DNS is validated and pinned on every redirect hop."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .links import normalize_link

USER_AGENT = "AIRadar/0.3 (personal source reader)"


class PageUnavailable(Exception):
    def __init__(self, status: str, message: str):
        self.status, self.message = status, message
        super().__init__(message)


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
                        if response.status_code in {401, 403}:
                            raise PageUnavailable("auth_required", "页面要求登录授权或限制自动读取，尚未取得正文。")
                        if response.status_code == 429:
                            raise PageUnavailable("rate_limited", "页面访问频率受限，稍后重试。")
                        response.raise_for_status()
                        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                            raise PageUnavailable("unsupported", "页面未提供可安全读取的未压缩响应。")
                        data = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            data.extend(chunk)
                            if len(data) > limit:
                                raise PageUnavailable("too_large", "页面或文件超过单次读取大小限制。")
                        return url, response.status_code, dict(response.headers), bytes(data)
            raise PageUnavailable("unavailable", "链接重定向过多，尚未取得正文。")

    async def check_robots(self, url: str):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            parser = RobotFileParser(origin + "/robots.txt")
            try:
                _, _, _, data = await self.bytes(parser.url, limit=150000, check_robots=False)
                parser.parse(data.decode("utf-8", errors="replace").splitlines())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {404, 410}:
                    parser.parse([])
                else:
                    raise PageUnavailable("unavailable", "暂时无法确认页面的自动读取规则，稍后重试。") from exc
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch("AIRadar", url):
            raise PageUnavailable("restricted", "网站不允许自动读取该页面，未抓取正文。")
