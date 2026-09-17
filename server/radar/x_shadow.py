"""One bounded daily Apify sample, never a fallback for every primary failure.

Only metadata comparisons are saved. It does not launch models or import duplicate
articles. Lost POST responses remain uncertain; they never trigger another run.
"""
from datetime import timedelta

from sqlalchemy import func, select

from .models import Article, XDataCall
from .sources import SourceUnavailable, x_page_items
from .x_collection import XCollectionResult, normalized_handles, stamp
from .x_costs import XCostLedger
from .x_data_config import vendor_secret
from .x_providers import vendor_page

APIFY = "https://api.apify.com/v2"
ACTOR = "xquik~x-tweet-scraper"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyShadow:
    def __init__(self, sessions, config, *, clock=None):
        self.sessions, self.config = sessions, config
        self.ledger = XCostLedger(sessions, config, clock=clock)

    async def collect(self, client, handles):
        result = XCollectionResult(status="partial")
        if not self.config.x_data.shadow_enabled:
            result.message = "Apify 对照未启用，没有发起付费请求。"
            return result
        token = vendor_secret(self.config, self.config.x_data.shadow_api_key_env)
        if not token:
            raise SourceUnavailable("auth_required", "Apify 对照需要 APIFY_API_TOKEN，主采集不受影响。")
        headers = {"Authorization": f"Bearer {token}"}
        with self.sessions() as session:
            pending = session.scalar(select(XDataCall).where(XDataCall.provider == "apify",
                XDataCall.status.in_(("reserved", "unknown"))).order_by(XDataCall.created_at).limit(1))
            pending_id = pending.id if pending else None
            pending_details = dict(pending.details) if pending else {}
        if pending_id:
            run_id = pending_details.get("run_id")
            if not run_id:
                raise SourceUnavailable("error", "Apify 启动结果不明，已保留费用预留并暂停新对照任务，需核对平台运行记录。")
            response = await client.get(f"{APIFY}/actor-runs/{run_id}", headers=headers)
            response.raise_for_status()
            run = response.json()["data"]
            if run.get("status") not in TERMINAL:
                result.message = "Apify 对照任务尚在运行，下轮仅检查同一任务，不重复启动。"
                return result
            # Authentication ensures this is the owner's total including platform costs.
            cost = run.get("usageTotalUsd")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
                raise SourceUnavailable("error", "Apify 尚未提供运行费用，继续保留预留且不启动新任务。")
            self.ledger.finish(pending_id, cost_usd=cost + 0.002, status="estimated",
                               details={"run_cost_usd": cost, "run_status": run["status"]})
            dataset = run.get("defaultDatasetId")
            if not isinstance(dataset, str) or not dataset.isalnum():
                raise ValueError("Invalid Apify result dataset")
            data = await client.get(f"{APIFY}/datasets/{dataset}/items",
                params={"format": "json", "clean": "true", "limit": 61}, headers=headers)
            data.raise_for_status()
            rows = data.json()
            if not isinstance(rows, list) or len(rows) > 60:
                raise ValueError("Apify sample exceeded its item cap")
            tweets = [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
                      and r.get("type", "tweet") == "tweet"]
            items = x_page_items(vendor_page(tweets))
            start, end = pending_details["start"], pending_details["end"]
            selected = set(pending_details["handles"])
            valid = [p for p in items if p.handle.lower() in selected
                     and start <= stamp(p.published_at) < end]
            with self.sessions() as session:
                primary_ids = set(session.scalars(select(Article.external_id).where(Article.platform == "x",
                    func.lower(Article.handle).in_(selected), Article.published_at >= start.replace("Z", "+00:00"),
                    Article.published_at < end.replace("Z", "+00:00"))))
            sampled_ids = {p.external_id for p in valid}
            comparison = {"sampled": len(sampled_ids), "overlap": len(sampled_ids & primary_ids),
                "sample_only": len(sampled_ids - primary_ids), "primary_only": len(primary_ids - sampled_ids),
                "sample_only_ids": sorted(sampled_ids - primary_ids)[:20],
                "outside_scope": len(items) - len(valid), "run_status": run["status"],
                "capped": len(tweets) >= 60, "note": "有限样本交集，不能视为完整漏帖率"}
            # Keep a small allowance for result reads/storage not covered by the run invoice.
            self.ledger.finish(pending_id, cost_usd=cost + 0.002, returned=len(tweets),
                status="estimated", details={"comparison": comparison, "run_cost_usd": cost})
            result.read_count = len(valid)
            result.committed_pages = 1
            result.status = "partial" if run["status"] != "SUCCEEDED" or comparison["outside_scope"] else "healthy"
            result.message = (f"Apify 抽样 {len(valid)} 条，与已入库内容重合 {comparison['overlap']} 条，"
                              f"仅抽样取得 {comparison['sample_only']} 条；仅用于对照，不触发模型调用。"
                              f"任务账单 ${cost:.4f}，另保留 $0.002 结果读取余量。有限样本不代表完整漏帖率。")
            return result
        handles = normalized_handles(handles)
        if not handles:
            result.message = "没有可供对照的关注账号。"
            return result
        now = self.ledger.clock()
        offset = now.date().toordinal() * 3 % len(handles)
        chosen = (handles + handles)[offset:offset + min(3, len(handles))]
        end = now - timedelta(minutes=10)
        start = end - timedelta(hours=1)
        details = {"handles": chosen, "start": stamp(start), "end": stamp(end)}
        call_id = self.ledger.reserve("apify", "daily_comparison", 0.1, details, once_per_day=True)
        if not call_id:
            result.message = "今日已启动过 Apify 对照，本轮不重复付费。"
            return result
        try:
            response = await client.post(f"{APIFY}/acts/{ACTOR}/runs", headers=headers,
                params={"timeout": 120, "memory": 256, "maxTotalChargeUsd": 0.02, "waitForFinish": 0},
                json={"searchTerms": [f"from:{h} -filter:retweets since_time:{int(start.timestamp())} "
                                      f"until_time:{int(end.timestamp())}" for h in chosen],
                      "queryType": "Latest", "maxItems": 60, "maxItemsPerTarget": 20})
            response.raise_for_status()
            run = response.json()["data"]
            run_id = run.get("id")
            if not isinstance(run_id, str) or not run_id.isalnum():
                raise ValueError("Invalid Apify run ID")
            self.ledger.update_details(call_id, run_id=run_id)
        except BaseException:
            self.ledger.finish(call_id, status="unknown")
            raise
        result.message = "已启动今日 Apify 小样本对照：最多 3 个账号、60 条、120 秒；下轮读取同一任务。"
        return result
