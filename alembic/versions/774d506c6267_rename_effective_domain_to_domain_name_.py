"""rename effective_domain to domain_name in notification template content_config

Renames ``{{ effective_domain }}`` → ``{{ domain_name }}`` inside the
``content_config`` JSONB column across all three notification-config tables.
The key appears in user-authored Jinja2 ``title_template`` / ``body_template``
strings stored under ``default`` and any per-event ``overrides``.  The
code-side rename (resolution.py, content.py) happened in #177; this migration
keeps stored template bodies in sync.

Revision ID: 774d506c6267
Revises: db488115530a
Create Date: 2026-05-27 17:05:54.311929

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "774d506c6267"
down_revision: str | Sequence[str] | None = "db488115530a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [
    "notification_templates",
    "watch_notification_configs",
    "watched_item_notification_templates",
]


def upgrade() -> None:
    """Replace '{{ effective_domain }}' → '{{ domain_name }}' in stored templates."""
    for table in _TABLES:
        op.execute(
            f"""
            UPDATE {table}
            SET content_config = replace(
                content_config::text,
                '{{{{ effective_domain }}}}',
                '{{{{ domain_name }}}}'
            )::jsonb
            WHERE content_config::text LIKE '%{{{{ effective_domain }}}}%'
            """
        )


def downgrade() -> None:
    """Revert '{{ domain_name }}' → '{{ effective_domain }}' in stored templates."""
    for table in _TABLES:
        op.execute(
            f"""
            UPDATE {table}
            SET content_config = replace(
                content_config::text,
                '{{{{ domain_name }}}}',
                '{{{{ effective_domain }}}}'
            )::jsonb
            WHERE content_config::text LIKE '%{{{{ domain_name }}}}%'
            """
        )
