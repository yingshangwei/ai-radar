"""Read-only freshness ranking; selection never alters a saved model decision."""
import math
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def day_start(config, now):
    zone = ZoneInfo(config.timezone) if config.discovery.freshness_enabled else UTC
    return now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def deadline(row, config):
    published = datetime.fromisoformat(row.payload['published_at'])
    limit = published + timedelta(hours=config.discovery.max_age_hours)
    if not config.discovery.freshness_enabled:
        return limit
    created = datetime.fromisoformat(row.created_at)
    next_day = (created.astimezone(ZoneInfo(config.timezone)) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return min(limit, created + timedelta(minutes=config.discovery.max_wait_minutes), next_day.astimezone(UTC))


def score(row, now):
    published = datetime.fromisoformat(row.payload['published_at'])
    age = max(0, (now - published).total_seconds() / 3600)
    text = row.payload.get('text', '')
    signal = bool(re.search(r'\b(releas\w*|launch\w*|paper|benchmark|weights|dataset|research)\b|发布|开源|论文|突破', text, re.I))
    entities = bool(row.payload.get('entities'))
    return (40 * bool(row.source_priority) + 20 * signal + 10 * entities
            + min(15, math.log1p(max(0, row.latest_engagement)) * 2)
            + 30 / (1 + age) + 8 * bool(row.low_engagement))


def ordered(rows, config, now=None):
    now = now or datetime.now(UTC)
    if not config.discovery.freshness_enabled:
        return sorted(rows, key=lambda r: (r.created_at, r.id))
    return sorted(rows, key=lambda r: (r.status != 'accepted', -score(r, now),
                                      -datetime.fromisoformat(r.payload['published_at']).timestamp(), r.id))
