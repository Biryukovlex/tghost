"""Explicit source links and complete draft version snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_simple_drafts"
down_revision = "0020_channel_metrics"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("studio_drafts", sa.Column("source_links", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("studio_draft_versions", sa.Column("working_title", sa.Text(), nullable=True))
    op.add_column("studio_draft_versions", sa.Column("source_links", JSONB(), nullable=True))
    # Recover links from the conversation's own evidence. Old versions stored
    # only body/media, so only the active version's metadata is knowable.
    op.execute("""UPDATE studio_drafts d SET source_links = COALESCE((
        SELECT jsonb_agg(DISTINCT s.url)
        FROM studio_sources s
        WHERE s.workspace_id=d.workspace_id AND s.conversation_id=d.conversation_id
          AND d.source_ids ? s.id AND s.url ~ '^https?://'
    ), '[]'::jsonb)""")
    op.execute("""UPDATE studio_draft_versions v
        SET working_title=d.working_title, source_links=d.source_links
        FROM studio_drafts d WHERE d.workspace_id=v.workspace_id
          AND d.id=v.draft_id AND d.current_version=v.version""")


def downgrade():
    op.drop_column("studio_draft_versions", "source_links")
    op.drop_column("studio_draft_versions", "working_title")
    op.drop_column("studio_drafts", "source_links")
