"""Conservative, durable spending guard for third-party X collection.

Units are integer millionths of USD. Unknown requests keep their reservation;
neither process restarts nor a failed article transaction refund network calls.
"""
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from .models import XDataCall
from .sources import SourceUnavailable

NAMES = {"official": "X 官方 API", "twitterapi_io": "TwitterAPI.io", "apify": "Apify / Xquik"}
CONSOLES = {"official": "https://console.x.com/", "twitterapi_io": "https://twitterapi.io/dashboard",
            "apify": "https://console.apify.com/"}


def micros(value):
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class XCostLedger:
    def __init__(self, sessions, config, *, clock=None):
        self.sessions, self.config = sessions, config
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def transaction(self):
        with self.sessions() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def periods(self):
        day = self.clock().astimezone(ZoneInfo(self.config.timezone)).date().isoformat()
        return day, day[:7]

    def limits(self, provider):
        c = self.config.x_data
        return (c.shadow_daily_usd, c.shadow_monthly_usd) if provider == "apify" else (c.daily_usd, c.monthly_usd)

    def reserve(self, provider, purpose, maximum_usd, details=None, *, once_per_day=False):
        amount = micros(maximum_usd)
        if amount <= 0:
            raise ValueError("A paid request must reserve its maximum expected charge")
        day, month = self.periods()
        daily, monthly = self.limits(provider)
        with self.transaction() as session:
            if once_per_day and session.scalar(select(XDataCall.id).where(
                    XDataCall.provider == provider, XDataCall.day == day).limit(1)):
                return None
            if session.scalar(select(XDataCall.id).where(XDataCall.provider == provider,
                    XDataCall.status == "price_mismatch").limit(1)):
                raise SourceUnavailable("budget_exhausted", "供应商费用超出预留值，已停止付费请求，需核验价格配置。")
            query = select(func.coalesce(func.sum(XDataCall.cost_microusd), 0)).where(
                XDataCall.provider == provider)
            spent_day = session.scalar(query.where(XDataCall.day == day))
            spent_month = session.scalar(query.where(XDataCall.month == month))
            if spent_day + amount > micros(daily) or spent_month + amount > micros(monthly):
                raise SourceUnavailable("budget_exhausted",
                    f"{NAMES[provider]} 已达到费用保护线或剩余额度不足以预留下一次请求。"
                    f"今日 ${spent_day / 1e6:.4f}/${daily:g}，本月 ${spent_month / 1e6:.4f}/${monthly:g}。"
                    "包含结果不明的费用预留；下个预算周期自动恢复，不自动充值或切换供应商。")
            row = XDataCall(provider=provider, purpose=purpose, day=day, month=month,
                reserved_microusd=amount, cost_microusd=amount, details=details or {})
            session.add(row)
            session.flush()
            return row.id

    def finish(self, call_id, *, cost_usd=None, returned=0, status="estimated", details=None):
        with self.transaction() as session:
            row = session.get(XDataCall, call_id)
            if cost_usd is not None:
                row.cost_microusd = micros(cost_usd)
            row.status = "price_mismatch" if row.cost_microusd > row.reserved_microusd else status
            row.returned = returned
            row.details = {**row.details, **(details or {})}

    def update_details(self, call_id, **details):
        with self.transaction() as session:
            row = session.get(XDataCall, call_id)
            row.details = {**row.details, **details}

    def accepted(self, session, call_id, count):
        if call_id:
            session.get(XDataCall, call_id).accepted = count

    def report(self):
        day, month = self.periods()
        result = []
        with self.sessions() as session:
            for provider in ("twitterapi_io", "apify"):
                daily, monthly = self.limits(provider)
                q = select(func.coalesce(func.sum(XDataCall.cost_microusd), 0), func.count(),
                           func.coalesce(func.sum(XDataCall.returned), 0),
                           func.coalesce(func.sum(XDataCall.accepted), 0)).where(XDataCall.provider == provider)
                total = session.execute(q.where(XDataCall.month == month)).one()
                today = session.execute(q.where(XDataCall.day == day)).one()
                unknown = session.scalar(select(func.count()).select_from(XDataCall).where(
                    XDataCall.provider == provider, XDataCall.month == month,
                    XDataCall.status.in_(("reserved", "unknown"))))
                result.append({"provider": provider, "name": NAMES[provider], "daily_usd": daily,
                    "monthly_usd": monthly, "today_usd": today[0] / 1e6, "month_usd": total[0] / 1e6,
                    "requests": total[1], "returned": total[2], "accepted": total[3],
                    "uncertain_requests": unknown, "estimated": provider == "twitterapi_io",
                    "console_url": CONSOLES[provider]})
        provider = self.config.x_data.provider
        return {"provider": provider, "name": NAMES[provider], "console_url": CONSOLES[provider],
                "timezone": self.config.timezone, "month": month, "providers": result,
                "automatic_fallback": False, "shadow_enabled": self.config.x_data.shadow_enabled}
