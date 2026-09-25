"""Add durable RepoPilot runtime tables to PostgreSQL."""
from alembic import op
import sqlalchemy as sa

revision = "0002_runtime_tables"
down_revision = "0001_auth_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("sessions", sa.Column("id", sa.Text(), primary_key=True), sa.Column("source", sa.Text(), nullable=False), sa.Column("name", sa.Text(), nullable=False), sa.Column("kind", sa.Text(), nullable=False), sa.Column("status", sa.Text(), nullable=False), sa.Column("error", sa.Text()), sa.Column("analysis", sa.Text()), sa.Column("base_commit", sa.Text()), sa.Column("owner", sa.Text(), nullable=False, server_default=""), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_table("runs", sa.Column("id", sa.Text(), primary_key=True), sa.Column("session_id", sa.Text(), nullable=False), sa.Column("task", sa.Text(), nullable=False), sa.Column("approval_mode", sa.Text(), nullable=False), sa.Column("llm_kind", sa.Text(), nullable=False), sa.Column("model", sa.Text()), sa.Column("status", sa.Text(), nullable=False), sa.Column("summary", sa.Text()), sa.Column("error", sa.Text()), sa.Column("steps", sa.Integer(), nullable=False, server_default="0"), sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"), sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"), sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"), sa.Column("files_changed", sa.Integer(), nullable=False, server_default="0"), sa.Column("lines_changed", sa.Integer(), nullable=False, server_default="0"), sa.Column("tests_passed", sa.Integer()), sa.Column("diff", sa.Text()), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("finished_at", sa.Float()))
    op.create_table("events", sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True), sa.Column("run_id", sa.Text(), nullable=False), sa.Column("ts", sa.Float(), nullable=False), sa.Column("kind", sa.Text(), nullable=False), sa.Column("data", sa.Text(), nullable=False))
    op.create_index("idx_events_run", "events", ["run_id", "id"])
    op.create_table("approvals", sa.Column("id", sa.Text(), primary_key=True), sa.Column("run_id", sa.Text(), nullable=False), sa.Column("tool", sa.Text(), nullable=False), sa.Column("args", sa.Text(), nullable=False), sa.Column("preview", sa.Text()), sa.Column("reason", sa.Text()), sa.Column("status", sa.Text(), nullable=False), sa.Column("note", sa.Text()), sa.Column("created_at", sa.Float(), nullable=False), sa.Column("decided_at", sa.Float()))


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_index("idx_events_run", table_name="events")
    op.drop_table("events")
    op.drop_table("runs")
    op.drop_table("sessions")
