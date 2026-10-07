# Channel statistics in Overview

Select one channel to see current Subscribers, Typical views, ER and ERR.
Typical views has a 24h / 48h / 72h switcher. Each card selects its recorded
history in the existing chart. Dates restrict observation history but do not
change the current channel cards. Post totals keep their publication-date
scope, Daily posts / Cumulative in window modes and Day / Week / Month grouping.
Search, filters, reading, pagination and archive exports are unchanged.

## Definitions and collection

- **Subscribers:** the last measured Telegram participant count. Its timestamp
  is shown separately; a failed lookup retains the last observation with its
  original timestamp rather than pretending it was refreshed.
- **Typical views:** mean measured views at the selected post age for posts
  published in the preceding 30 days and old enough for that horizon.
  Measurements occur from the horizon to 60 minutes afterwards. Collection
  records the actual timestamp and preserves the first qualifying measurement.
  Coverage shows measured / eligible posts; a partial sample is not the whole
  archive. Polling less often than hourly or outages can miss measurements.
- **ER:** `(reactions + comments + forwards) / views * 100`, using the same
  30-day post sample and each post's latest stored counters.
- **ERR:** `average current post views / measured subscribers * 100`, using
  that same recent sample. Fixed-age views are separate from current views.

ER is unavailable without a positive view denominator. ERR is unavailable
without posts and a positive subscriber count. Unknown metrics show a dash;
unknown history remains a gap. The UI compares observations at least 30 days
apart when a baseline exists; rate changes are percentage points.

Channel statistics are never cumulatively summed. Weekly/monthly charts use
latest observations in each bucket. All-active-channel Overview retains its
legacy totals; select a single channel for channel-specific statistics.

Each channel scan fetches a participant count and records a metric observation.
Targeted reads (up to 200 due archived messages per cycle, batches of 100)
cover 24/48/72-hour measurements even when the ordinary scan visits only recent
posts. Missing Telegram view counters never become zero horizon measurements.
Failures in optional statistics are isolated; Telegram flood waits continue to
use the collector's existing backoff behavior. Very busy channels can exceed
the bounded targeted-read capacity; their sample coverage remains explicit.

The ratio meanings follow the requested TGStat definitions. The 30-day sample,
weighted ER aggregation and collection tolerance are TGhost conventions; exact
TGStat numerical equivalence is not asserted.

## Upgrade and historical availability

Apply Alembic revision `0020_channel_metrics` before running the updated app.
It adds workspace-scoped `post_view_horizons` and
`channel_metric_observations`, linked by composite foreign keys. Existing
Telegram post snapshots and channel data remain untouched.

Subscriber history and channel ratios begin with new collection observations.
Fixed-age history is not reconstructed from archived lifetime counters. Older
or missed 24/48/72-hour measurements remain unavailable. No initial fake values,
interpolation, or retrospective subscriber guesses are inserted.
