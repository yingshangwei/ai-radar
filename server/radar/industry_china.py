"""Domestic company disclosures and policy: fixed official publishers, bounded reads."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .page_parser import extract_page

# IDs were cross-checked against CNINFO's official stockList, not inferred from names.
ISSUERS = (
    ("inspur", "浪潮信息", "000977", "SZSE", "gssz0000977", ["infrastructure"]),
    ("iflytek", "科大讯飞", "002230", "SZSE", "9900004565", ["software", "workflows"]),
    ("e-evik", "英维克", "002837", "SZSE", "9900029666", ["infrastructure"]),
    ("innolight", "中际旭创", "300308", "SZSE", "9900022016", ["infrastructure"]),
    ("yonyou", "用友网络", "600588", "SSE", "gssh0600588", ["software", "workflows"]),
    ("hygon", "海光信息", "688041", "SSE", "9900048365", ["infrastructure"]),
    ("kingsoft-office", "金山办公", "688111", "SSE", "9900035303", ["software", "workflows"]),
    ("cambricon", "寒武纪", "688256", "SSE", "nssc1000595", ["infrastructure"]),
)
FINANCIAL = re.compile(
    r"年度报告|半年度报告|季度报告|業績|业绩|財務|财务|中期報告|年度報告|季度.*業績|重大合同|战略合作|戰略合作|配售|发行.*预案|發行.*預案|经营情况|經營情況"
)
NOISE = re.compile(r"制度|管理办法|章程|议事规则|監事|监事")
POLICY = re.compile(
    r"人工智能|算力|数据中心|数据要素|数字经济|半导体|集成电路|智能制造|机器人|新型电力|电网|电价|能源体系|节能降碳|工业品"
)


def register(entities, sources, spec):
    for uid, name, ticker, exchange, _org, themes in ISSUERS:
        entities[uid] = dict(
            id=uid, name=name, ticker=ticker, exchange=exchange, cik=None, themes=themes, region="cn"
        )
        sources["cninfo-" + uid] = spec(
            "cninfo-" + uid,
            "巨潮 · " + name,
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            "cn_filing",
            (uid,),
            "官方公告最近窗口与最多两份经营/定期报告 PDF 摘录。",
            "公开网站接口，无全历史/SLA；最多两页，PDF 最多40页/60000字；摘要不当成全文。",
            30,
        )
    entities["alibaba-cn"] = dict(
        id="alibaba-cn",
        name="阿里巴巴",
        ticker="09988",
        exchange="HKEX",
        cik=None,
        themes=["infrastructure", "software", "workflows"],
        region="cn",
    )
    sources["alibaba-hk"] = spec(
        "alibaba-hk",
        "阿里巴巴 · 中文港股公告",
        "https://www.alibabagroup.com/zh-HK/ir-filings-hkex",
        "cn_filing",
        ("alibaba-cn",),
        "公司官网列示的中文港交所公告及有限 PDF 摘录。",
        "仅官网当前窗口；不是港股全市场覆盖。",
        30,
    )
    sources["ndrc-policy"] = spec(
        "ndrc-policy",
        "国家发改委 · 产业与能源政策",
        "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
        "cn_policy",
        (),
        "AI、算力、制造、电力与产业成本相关通知。",
        "仅通知首页窗口，主题筛选；不代表全部国内政策。",
        30,
    )


def valid_url(url, hosts):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in hosts
        or p.username
        or p.password
        or p.port not in (None, 443)
        or "\\" in url
        or any(ord(c) < 32 for c in url)
    ):
        raise ValueError("Unexpected publisher URL")
    return url


async def request(client, url, hosts, *, data=None, pdf=False):
    valid_url(url, hosts)
    req = httpx.Request(
        "POST" if data is not None else "GET",
        url,
        data=data,
        headers={"User-Agent": "AI-Radar/1.0 personal research", "Accept-Encoding": "identity"},
    )
    async with asyncio.timeout(25):
        r = await client.send(req, stream=True, auth=None, follow_redirects=False)
        try:
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            if not any(t in ct for t in (("pdf", "octet-stream") if pdf else ("json", "html", "text/plain"))):
                raise ValueError("Invalid content type")
            maximum = 8_000_000 if pdf else 2_000_000
            raw = bytearray()
            async for chunk in r.aiter_bytes(65536):
                if len(raw) + len(chunk) > maximum:
                    raise ValueError("Response too large")
                raw.extend(chunk)
            if pdf and not raw.startswith(b"%PDF-"):
                raise ValueError("Not a PDF")
            return bytes(raw)
        finally:
            await r.aclose()


def date(value):
    # Date-only releases are anchored to local midnight, never the ingestion time.
    raw = re.sub(r"[年月/]", "-", value).replace("日", "").strip()
    parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(UTC)


def item(source, url, title, published, now, days):
    from .industry_sources import EvidenceInput

    if not now - timedelta(days=days) <= published <= now:
        return None
    return EvidenceInput(
        source.id,
        url,
        url,
        title,
        title,
        published.isoformat(),
        "date",
        "policy_notice" if source.kind == "cn_policy" else "filing_notice",
        list(source.entity_ids),
        {"partial": True, "history_complete": False, "evidence_scope": "headline_only", "region": "cn"},
    )


async def fetch_china(client, source, now, days, limit):
    from .industry_sources import IndustrySourceError

    rows = []
    try:
        if source.id.startswith("cninfo-"):
            uid = source.entity_ids[0]
            _, _, code, exchange, org, _ = next(row for row in ISSUERS if row[0] == uid)
            hosts = {"www.cninfo.com.cn", "static.cninfo.com.cn"}
            for page in (1, 2):
                payload = {
                    "pageNum": str(page),
                    "pageSize": "30",
                    "column": "szse" if exchange == "SZSE" else "sse",
                    "tabName": "fulltext",
                    "stock": code + "," + org,
                    "seDate": f"{(now - timedelta(days=days)).date()}~{now.astimezone(ZoneInfo('Asia/Shanghai')).date()}",
                    "sortName": "time",
                    "sortType": "desc",
                    "isHLtitle": "false",
                }
                obj = json.loads(await request(client, source.url, hosts, data=payload))
                if not isinstance(obj, dict) or "announcements" not in obj or "totalAnnouncement" not in obj:
                    raise ValueError("Unknown announcement schema")
                for a in obj["announcements"] or []:
                    if str(a.get("secCode")) != code or a.get("orgId") != org:
                        continue
                    title = BeautifulSoup(a.get("announcementTitle", ""), "html.parser").get_text(
                        " ", strip=True
                    )
                    if not FINANCIAL.search(title) or NOISE.search(title):
                        continue
                    path = a.get("adjunctUrl", "")
                    if not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", path):
                        continue
                    # CNINFO announcementTime is date precision in a millisecond field.
                    stamp = a.get("announcementTime")
                    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
                        continue
                    published = date(
                        datetime.fromtimestamp(stamp / 1000, UTC)
                        .astimezone(ZoneInfo("Asia/Shanghai"))
                        .date()
                        .isoformat()
                    )
                    row = item(source, "https://static.cninfo.com.cn/" + path, title, published, now, days)
                    if row:
                        rows.append(row)
                if not obj.get("hasMore") or len(rows) >= limit:
                    break
                await asyncio.sleep(0.35)
        elif source.id == "alibaba-hk":
            hosts = {"www.alibabagroup.com", "data.alibabagroup.com"}
            soup = BeautifulSoup(await request(client, source.url, hosts), "html.parser")
            cards = soup.select(".filings.if-pc")
            if not cards:
                raise ValueError("No disclosure structure")
            for card in cards[:100]:
                a, stamp = card.select_one(".filings-title a[href]"), card.select_one(".filings-date")
                if not a or not stamp:
                    continue
                title = a.get_text(" ", strip=True)
                if not FINANCIAL.search(title) or NOISE.search(title):
                    continue
                url = valid_url(a["href"], {"data.alibabagroup.com"})
                if not urlsplit(url).path.startswith(
                    "/ir_filings/HKEX/09988/cn/"
                ) or not url.lower().endswith(".pdf"):
                    continue
                row = item(source, url, title, date(stamp.get_text(strip=True)), now, days)
                if row:
                    rows.append(row)
        else:
            hosts = {"www.ndrc.gov.cn"}
            soup = BeautifulSoup(await request(client, source.url, hosts), "html.parser")
            candidates = soup.select("li")
            dated = 0
            for card in candidates:
                stamp = re.search(r"\d{4}/\d{2}/\d{2}", card.get_text(" ", strip=True))
                if not stamp:
                    continue
                dated += 1
                for a in card.select("a[href]")[:1]:
                    title = a.get_text(" ", strip=True)
                    if not POLICY.search(title):
                        continue
                    url = valid_url(urljoin(source.url, a["href"]), hosts)
                    if not urlsplit(url).path.startswith("/xxgk/zcfb/tz/"):
                        continue
                    row = item(source, url, title, date(stamp.group()), now, days)
                    if row:
                        rows.append(row)
            if not dated:
                raise ValueError("No dated policy structure")
        rows = list({row.url: row for row in rows}.values())
        rows.sort(key=lambda row: row.published_at, reverse=True)
        rows = rows[:limit]
        # Bound publisher work; prioritise actual reports over notice-only items.
        reports = [r for r in rows if re.search(r"(?:年度|季度).*报告|業績|业绩", r.title)]
        reports.sort(key=lambda r: (r.published_at, "摘要" in r.title), reverse=True)
        targets = reports[:1] + [r for r in rows if r not in reports[:1]]
        for row in targets[:2]:
            try:
                is_pdf = row.url.lower().endswith(".pdf")
                body = await request(client, row.url, hosts, pdf=is_pdf)
                parsed = await extract_page(body, "application/pdf" if is_pdf else "text/html", row.url)
                row.text = parsed["text"]
                if not is_pdf and "附件" in row.text:
                    parsed["partial"] = True  # Linked policy attachments were not read.
                row.kind = "filing_document" if is_pdf else "policy"
                row.metadata.update(
                    partial=parsed["partial"],
                    evidence_scope="filing_primary_document" if is_pdf else "publisher_article",
                    document_fetch="ready",
                    includes_exhibits=False,
                    truncated=parsed["partial"],
                )
            except (httpx.HTTPError, ValueError, TimeoutError):
                row.metadata["document_fetch"] = "unavailable"
        return rows
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        code = "blocked" if status in (401, 403) else "rate_limited" if status == 429 else "invalid_response"
        raise IndustrySourceError(code, source.id + ": 国内官方来源暂不可用或结构校验失败") from None
