"""Workspace-scoped measured channel statistics for PostgresDatabase."""
import json
from datetime import datetime, timezone
from sqlalchemy import text
from .channel_metrics import HORIZONS, eligible_horizons, empty_metrics, metrics_payload, ratios


class ChannelMetricsRepository:
    async def record_post_horizon_views(self, post_id, views, observed_at=None):
        if views is None or int(views) < 0:
            return
        at = observed_at or datetime.now(timezone.utc)
        result = await self._execute('''SELECT posted_at FROM posts WHERE workspace_id=:workspace_id AND id=:post_id AND NOT is_deleted''', {'post_id':post_id})
        row = result.mappings().first()
        if not row:
            return
        for horizon in eligible_horizons(row['posted_at'], at):
            await self._execute('''INSERT INTO post_view_horizons(workspace_id,post_id,horizon_hours,taken_at,views)
                VALUES(:workspace_id,:post_id,:horizon,:at,:views) ON CONFLICT DO NOTHING''',
                {'post_id':post_id,'horizon':horizon,'at':at,'views':int(views)})

    async def pending_view_horizons(self, channel_id, observed_at=None):
        """Bounded targeted reads; includes posts outside the recent scan cutoff."""
        result = await self._execute('''SELECT DISTINCT p.id, p.message_id, p.posted_at
            FROM posts p CROSS JOIN (VALUES (24),(48),(72)) h(hours)
            WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id AND NOT p.is_deleted
              AND p.posted_at + h.hours * interval '1 hour' <= :at
              AND p.posted_at + h.hours * interval '1 hour' + interval '60 minutes' >= :at
              AND NOT EXISTS (SELECT 1 FROM post_view_horizons v WHERE v.workspace_id=p.workspace_id
                AND v.post_id=p.id AND v.horizon_hours=h.hours)
            ORDER BY p.posted_at LIMIT 200''', {'channel_id':channel_id,'at':observed_at or datetime.now(timezone.utc)})
        return [dict(row) for row in result.mappings()]

    async def capture_channel_metrics(self, channel_id, subscribers=None, observed_at=None):
        at = observed_at or datetime.now(timezone.utc)
        params = {'workspace_id':self._workspace(),'channel_id':channel_id,'at':at}
        async with self.sessions.session() as session:
            # The composite FK enforces channel provenance; a missing/foreign channel is never written.
            exists = (await session.execute(text('SELECT id FROM channels WHERE workspace_id=:workspace_id AND id=:channel_id'),params)).first()
            if not exists:
                return
            subscriber_at = at if subscribers is not None else None
            if subscribers is None:
                prior = (await session.execute(text('''SELECT subscribers,subscribers_observed_at FROM channel_metric_observations
                    WHERE workspace_id=:workspace_id AND channel_id=:channel_id AND subscribers IS NOT NULL
                    ORDER BY taken_at DESC,id DESC LIMIT 1'''),params)).mappings().first()
                if prior:
                    subscribers,subscriber_at = prior['subscribers'],prior['subscribers_observed_at']
            totals = (await session.execute(text('''SELECT COUNT(*) AS posts,COALESCE(SUM(s.views),0) AS views,
                    COALESCE(SUM(s.reactions+s.comments+s.shares),0) AS interactions
                FROM posts p JOIN LATERAL (SELECT views,reactions,comments,shares FROM snapshots
                    WHERE workspace_id=p.workspace_id AND post_id=p.id AND taken_at<=:at
                    ORDER BY taken_at DESC,id DESC LIMIT 1) s ON true
                WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id AND NOT p.is_deleted
                    AND p.posted_at>=:at-interval '30 days' AND p.posted_at<=:at'''),params)).mappings().one()
            values = ratios(int(totals['views']),int(totals['interactions']),int(totals['posts']),subscribers)
            coverage = {'posts':int(totals['posts'])}
            for horizon in HORIZONS:
                sample = (await session.execute(text('''SELECT COUNT(p.id) AS eligible,COUNT(v.post_id) AS measured,AVG(v.views) AS average
                    FROM posts p LEFT JOIN post_view_horizons v ON v.workspace_id=p.workspace_id
                        AND v.post_id=p.id AND v.horizon_hours=:horizon AND v.taken_at<=:at
                    WHERE p.workspace_id=:workspace_id AND p.channel_id=:channel_id AND NOT p.is_deleted
                        AND p.posted_at>=:at-interval '30 days' AND p.posted_at<=:at-:horizon * interval '1 hour'
                    '''),{**params,'horizon':horizon})).mappings().one()
                values[f'views{horizon}'] = float(sample['average']) if sample['average'] is not None else None
                coverage[f'views{horizon}'] = {'measured':int(sample['measured']),'eligible':int(sample['eligible'])}
            await session.execute(text('''INSERT INTO channel_metric_observations(workspace_id,channel_id,taken_at,subscribers,
                subscribers_observed_at,views24,views48,views72,er,err,coverage)
                VALUES(:workspace_id,:channel_id,:at,:subscribers,:subscriber_at,:views24,:views48,:views72,:er,:err,CAST(:coverage AS jsonb))'''),
                {**params,**values,'subscribers':subscribers,'subscriber_at':subscriber_at,'coverage':json.dumps(coverage)})
            await session.commit()

    async def channel_metrics(self, channel_id=None, from_date=None, to_date=None):
        if channel_id is None:
            return empty_metrics()
        # Last actual observation in each UTC day; never interpolate or substitute lifetime counters.
        result = await self._execute('''SELECT * FROM (
            SELECT DISTINCT ON ((taken_at AT TIME ZONE 'UTC')::date) * FROM channel_metric_observations
            WHERE workspace_id=:workspace_id AND channel_id=:channel_id
            ORDER BY (taken_at AT TIME ZONE 'UTC')::date,taken_at DESC,id DESC
        ) daily ORDER BY taken_at''', {'channel_id':channel_id})
        return metrics_payload([dict(r) for r in result.mappings()],from_date,to_date)
