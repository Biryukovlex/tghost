"""Channel observation contract; counts are measured, never backfilled from lifetime views."""
from datetime import datetime, timedelta, timezone

HORIZONS = (24, 48, 72)
HORIZON_GRACE_MINUTES = 60
SAMPLE_DAYS = 30
METRICS = ('subscribers', 'views24', 'views48', 'views72', 'er', 'err')


def eligible_horizons(posted_at: datetime, observed_at: datetime) -> tuple[int, ...]:
    age = observed_at - posted_at
    return tuple(h for h in HORIZONS if timedelta(hours=h) <= age <= timedelta(hours=h, minutes=HORIZON_GRACE_MINUTES))


def ratios(views: int, interactions: int, posts: int, subscribers: int | None):
    return {
        'er': interactions / views * 100 if views > 0 else None,
        'err': views / posts / subscribers * 100 if posts > 0 and subscribers and subscribers > 0 else None,
    }


def empty_metrics():
    return {'current': {key: None for key in METRICS}, 'previous': None,
            'series': {'labels': [], 'observed': [], **{key: [] for key in METRICS}},
            'sample_days': SAMPLE_DAYS, 'horizon_grace_minutes': HORIZON_GRACE_MINUTES,
            'observed_at': None, 'subscribers_observed_at': None, 'coverage': {}}


def metrics_payload(rows, from_date=None, to_date=None):
    """Latest daily observations; current cards do not follow the chart window."""
    result = empty_metrics()
    if not rows:
        return result
    current = rows[-1]
    result.update(current={key: current.get(key) for key in METRICS},
                  observed_at=current['taken_at'].isoformat(),
                  subscribers_observed_at=current['subscribers_observed_at'].isoformat() if current.get('subscribers_observed_at') else None,
                  coverage=current.get('coverage') or {})
    baseline = current['taken_at'] - timedelta(days=30)
    previous = next((r for r in reversed(rows) if r['taken_at'] <= baseline), None)
    if previous:
        result['previous'] = {key: previous.get(key) for key in METRICS}
    daily = {}
    for row in rows:
        day = row['taken_at'].astimezone(timezone.utc).date().isoformat()
        if (not from_date or day >= from_date) and (not to_date or day <= to_date):
            daily[day] = row
    labels = []
    if daily:
        day = datetime.fromisoformat(min(daily)).date()
        last = datetime.fromisoformat(max(daily)).date()
        while day <= last:
            labels.append(day.isoformat())
            day += timedelta(days=1)
    result['series'] = {'labels': labels, 'observed': [day in daily for day in labels],
                        **{key: [daily.get(day, {}).get(key) for day in labels] for key in METRICS}}
    return result
