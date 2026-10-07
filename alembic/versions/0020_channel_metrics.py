"""Measured subscriber and fixed-age views, with durable channel observations."""
from alembic import op
revision = '0020_channel_metrics'
down_revision = '0019_calendar_publishing'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE post_view_horizons (
        workspace_id UUID NOT NULL, post_id BIGINT NOT NULL,
        horizon_hours INTEGER NOT NULL CHECK(horizon_hours IN (24,48,72)),
        taken_at TIMESTAMPTZ NOT NULL, views BIGINT NOT NULL CHECK(views >= 0),
        PRIMARY KEY(workspace_id,post_id,horizon_hours),
        FOREIGN KEY(workspace_id,post_id) REFERENCES posts(workspace_id,id) ON DELETE CASCADE
    )''')
    op.execute('''CREATE TABLE channel_metric_observations (
        id BIGSERIAL PRIMARY KEY, workspace_id UUID NOT NULL, channel_id BIGINT NOT NULL,
        taken_at TIMESTAMPTZ NOT NULL, subscribers BIGINT CHECK(subscribers >= 0),
        subscribers_observed_at TIMESTAMPTZ,
        views24 DOUBLE PRECISION, views48 DOUBLE PRECISION, views72 DOUBLE PRECISION,
        er DOUBLE PRECISION, err DOUBLE PRECISION, coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
        FOREIGN KEY(workspace_id,channel_id) REFERENCES channels(workspace_id,id) ON DELETE CASCADE
    )''')
    op.execute('CREATE INDEX ix_channel_metrics_workspace_channel_time ON channel_metric_observations(workspace_id,channel_id,taken_at)')


def downgrade():
    op.execute('DROP TABLE channel_metric_observations')
    op.execute('DROP TABLE post_view_horizons')
