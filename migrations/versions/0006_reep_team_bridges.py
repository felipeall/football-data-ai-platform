"""Reep team bridges, next to the person bridges (ADR 34).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per bridge of a team Reep maps to at least two of the providers we ingest.
    # Reep IDs are already resolved through the release's redirects. Keyed by provider and
    # ID: bare numbers repeat across providers (Sofascore 83 is not Understat 83).
    op.execute(
        """
        CREATE TABLE reference.reep_team_bridges (
            release     text NOT NULL REFERENCES reference.reep_releases ON DELETE CASCADE,
            reep_id     text NOT NULL,
            provider    text NOT NULL CHECK (
                            provider IN ('sofascore', 'transfermarkt', 'understat')
                        ),
            external_id text NOT NULL,
            rule_id     text,
            route_scope text,
            PRIMARY KEY (release, provider, external_id, reep_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX reep_team_bridges_team_idx ON reference.reep_team_bridges (release, reep_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE reference.reep_team_bridges")
