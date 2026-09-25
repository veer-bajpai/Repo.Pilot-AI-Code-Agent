"""Add production identity, tenancy, usage, audit, and API key tables."""
from alembic import op
import sqlalchemy as sa

revision = "0001_auth_billing"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(320), nullable=False, unique=True), sa.Column("password_hash", sa.String(255)), sa.Column("name", sa.String(160), nullable=False, server_default=""), sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("plan", sa.String(32), nullable=False, server_default="free"), sa.Column("stripe_customer_id", sa.String(128)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table("oauth_identities", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("provider", sa.String(32), nullable=False), sa.Column("subject", sa.String(255), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_oauth_identities_user_id", "oauth_identities", ["user_id"])
    op.create_table("refresh_tokens", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table("email_tokens", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("purpose", sa.String(32), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("used_at", sa.DateTime(timezone=True)))
    op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"])
    op.create_table("teams", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(160), nullable=False), sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("team_members", sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True), sa.Column("role", sa.String(16), nullable=False, server_default="member"))
    op.create_table("usage_ledger", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("run_id", sa.String(64)), sa.Column("kind", sa.String(32), nullable=False, server_default="run"), sa.Column("amount", sa.Integer(), nullable=False, server_default="1"), sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_usage_ledger_user_id", "usage_ledger", ["user_id"])
    op.create_table("audit_logs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36)), sa.Column("action", sa.String(80), nullable=False), sa.Column("resource_type", sa.String(80), nullable=False), sa.Column("resource_id", sa.String(128)), sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_table("api_keys", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("key_hash", sa.String(64), nullable=False, unique=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])


def downgrade() -> None:
    for table in ("api_keys", "audit_logs", "usage_ledger", "team_members", "teams", "email_tokens", "refresh_tokens", "oauth_identities", "users"):
        op.drop_table(table)
