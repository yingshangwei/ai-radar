"""Discover only references explicitly present in the original source."""
import re
from html import unescape
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

SOCIAL_HOSTS = {"x.com", "twitter.com", "facebook.com", "instagram.com", "linkedin.com"}
ASSETS = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|mp4|mp3|zip|exe|dmg|apk|ipa|css|js)(?:$|\?)", re.I)
UTILITY = re.compile(r"(?:^|/)(?:login|sign-?in|sign-?up|logout|privacy|terms|cookie|search|feed)(?:/|$)", re.I)


def normalize_link(value: str, base: str = "") -> str | None:
    try:
        value = unescape(value.strip())
        if not value or value.startswith("#") or len(value) > 4000:
            return None
        value = urljoin(base, value)
        p = urlsplit(value)
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                or p.port not in {None, 80 if p.scheme == "http" else 443}
                or "\\" in value or any(ord(c) < 32 for c in value)):
            return None
        # Preserve exact query encoding (including signed public URLs); remove only tracking keys.
        query = [part for part in p.query.split("&") if not
                 (unquote_plus(part.split("=", 1)[0]).lower().startswith("utm_") or
                  unquote_plus(part.split("=", 1)[0]).lower() in {"fbclid", "gclid"})]
        clean = urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", "&".join(query), ""))
        return str(httpx.URL(clean))
    except (ValueError, httpx.InvalidURL):
        return None


def useful_link(url: str) -> bool:
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.").removeprefix("mobile.")
    return host not in SOCIAL_HOSTS and not ASSETS.search(url) and not UTILITY.search(p.path)


def text_references(text: str) -> list[dict]:
    refs = []
    for raw in re.findall(r"https?://[^\s<>\[\]，。！？、；：\"“”]+", text):
        raw = raw.rstrip(".,;:!?。！？，；：'’\"")
        while raw.endswith(")") and raw.count(")") > raw.count("("):
            raw = raw[:-1]
        url = normalize_link(raw)
        if url:
            refs.append({"url": url, "label": ""})
    return refs


def html_references(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("nav,header,footer,aside,script,style,form"):
        node.decompose()
    result = []
    for a in soup.select("a[href]"):
        url = normalize_link(a.get("href", ""), base)
        if url and useful_link(url):
            result.append({"url": url, "label": a.get_text(" ", strip=True)[:300]})
    return list({r["url"]: r for r in result}.values())[:30]


def mentioned_references(text: str, catalog: dict[str, str]) -> list[dict]:
    return [{"url": url, "label": name, "relation": "mention"} for name, url in catalog.items()
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.I)]
