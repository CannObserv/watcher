"""purge seeded watch_nc_refs for global and domain defaults

Revision ID: 07fe64d2fbc3
Revises: 24a8f66301be6728f318
Create Date: 2026-04-12 00:41:19.569505

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07fe64d2fbc3'
down_revision: Union[str, Sequence[str], None] = '24a8f66301be6728f318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove WatchNcRef rows that were seeded by _assign_default_templates.

    Global and domain templates now dispatch via live lookup in notify.py,
    so pre-seeded WatchNcRef rows for these templates are redundant and would
    cause duplicate notifications. This migration removes them.

    Global seeds: WatchNcRef rows whose template has is_global_default=True.
    Domain seeds: WatchNcRef rows whose template is a DomainNcRef default for
                  the watch's own effective_domain.
    """
    op.execute(
        """
        DELETE FROM watch_nc_refs
        WHERE template_id IN (
            SELECT id FROM notification_templates WHERE is_global_default = TRUE
        )
        """
    )
    op.execute(
        """
        DELETE FROM watch_nc_refs wnr
        WHERE EXISTS (
            SELECT 1
            FROM domain_nc_refs dnr
            JOIN watches w ON w.effective_domain = dnr.domain_name
            WHERE wnr.watch_id = w.id
              AND wnr.template_id = dnr.template_id
        )
        """
    )


def downgrade() -> None:
    """No downgrade — seeded rows are not recoverable without re-running watch creation."""
    pass
