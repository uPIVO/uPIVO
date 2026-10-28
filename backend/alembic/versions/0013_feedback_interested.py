"""Переименование положительной реакции обратной связи `useful` → `interested`.

Миграция меняет только значение enum `feedback_type`: строки `feedback`, их `id`
и отметки времени не переписываются, данные не теряются. `0012_feedback` уже
применена на рабочих базах и остаётся неизменной, поэтому переименование
оформлено отдельной ревизией. Откат возвращает прежнее имя значения.

Revision ID: 0013_feedback_interested
Revises: 0012_feedback
"""

from alembic import op

revision = "0013_feedback_interested"
down_revision = "0012_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE feedback_type RENAME VALUE 'useful' TO 'interested'")


def downgrade() -> None:
    op.execute("ALTER TYPE feedback_type RENAME VALUE 'interested' TO 'useful'")
