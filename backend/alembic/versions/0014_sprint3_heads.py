"""Join independently released favorites and interests/feedback revisions.

No existing revision or user data is rewritten.
"""

revision = "0014_sprint3_heads"
down_revision = ("0011_favorites", "0013_feedback_interested")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
