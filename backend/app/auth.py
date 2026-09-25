"""Production identity, tenant, quota, billing, and audit persistence."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailToken(Base):
    __tablename__ = "email_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class TeamMember(Base):
    __tablename__ = "team_members"
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="member")


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="run")
    amount: Mapped[int] = mapped_column(Integer, default=1)
    cost_usd: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value < _now()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        _, rounds, salt, digest = encoded.split("$", 3)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(), digest)
    except (AttributeError, ValueError):
        return False


def send_email(host: str, port: int, username: str, password: str, sender: str, recipient: str, subject: str, body: str, tls: bool = True) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, recipient, subject
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as server:
        if tls:
            server.starttls()
        if username:
            server.login(username, password)
        server.send_message(message)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def issue_jwt(user: User, secret: str, minutes: int) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"sub": user.id, "email": user.email, "exp": now + minutes * 60, "iat": now}, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64(hmac.new(secret.encode(), signing, hashlib.sha256).digest())}"


def verify_jwt(token: str, secret: str) -> dict[str, Any] | None:
    try:
        header, payload, signature = token.split(".")
        expected = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data if int(data.get("exp", 0)) > int(time.time()) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class AuthStore:
    def __init__(self, database_url: str, *, auto_create: bool = False):
        self.engine = create_engine(database_url, pool_pre_ping=True)
        if auto_create:
            Base.metadata.create_all(self.engine)

    def user(self, user_id: str) -> User | None:
        with Session(self.engine) as session:
            return session.get(User, user_id)

    def create_user(self, email: str, password: str, name: str = "") -> tuple[User, str]:
        email = email.strip().lower()
        with Session(self.engine) as session:
            if session.scalar(select(User).where(User.email == email)):
                raise ValueError("An account with that email already exists.")
            user = User(email=email, password_hash=hash_password(password), name=name.strip())
            session.add(user)
            session.flush()
            raw = secrets.token_urlsafe(32)
            session.add(EmailToken(user_id=user.id, purpose="verify", token_hash=_hash(raw), expires_at=_now() + timedelta(hours=24)))
            session.commit()
            session.refresh(user)
            return user, raw

    def authenticate(self, email: str, password: str) -> User | None:
        with Session(self.engine) as session:
            user = session.scalar(select(User).where(User.email == email.strip().lower()))
            return user if user and verify_password(password, user.password_hash) else None

    def oauth_user(self, provider: str, subject: str, email: str, name: str = "") -> User:
        with Session(self.engine) as session:
            identity = session.scalar(select(OAuthIdentity).where(OAuthIdentity.provider == provider, OAuthIdentity.subject == subject))
            if identity:
                user = session.get(User, identity.user_id)
                if user:
                    return user
            user = session.scalar(select(User).where(User.email == email.lower()))
            if not user:
                user = User(email=email.lower(), name=name, email_verified=True)
                session.add(user)
                session.flush()
            session.add(OAuthIdentity(user_id=user.id, provider=provider, subject=subject))
            session.commit()
            session.refresh(user)
            return user

    def create_email_token(self, email: str, purpose: str) -> str | None:
        with Session(self.engine) as session:
            user = session.scalar(select(User).where(User.email == email.strip().lower()))
            if not user:
                return None
            raw = secrets.token_urlsafe(32)
            session.add(EmailToken(user_id=user.id, purpose=purpose, token_hash=_hash(raw), expires_at=_now() + timedelta(hours=1)))
            session.commit()
            return raw

    def reset_password(self, raw: str, password: str) -> User | None:
        with Session(self.engine) as session:
            token = session.scalar(select(EmailToken).where(EmailToken.token_hash == _hash(raw), EmailToken.purpose == "reset"))
            if not token or token.used_at or _expired(token.expires_at):
                return None
            user = session.get(User, token.user_id)
            if not user:
                return None
            user.password_hash = hash_password(password)
            token.used_at = _now()
            session.commit()
            return user

    def consume_email_token(self, raw: str, purpose: str) -> User | None:
        with Session(self.engine) as session:
            token = session.scalar(select(EmailToken).where(EmailToken.token_hash == _hash(raw), EmailToken.purpose == purpose))
            if not token or token.used_at or _expired(token.expires_at):
                return None
            token.used_at = _now()
            user = session.get(User, token.user_id)
            if user and purpose == "verify":
                user.email_verified = True
            session.commit()
            if user:
                session.refresh(user)
            return user

    def issue_refresh(self, user_id: str, days: int) -> str:
        raw = secrets.token_urlsafe(48)
        with Session(self.engine) as session:
            session.add(RefreshToken(user_id=user_id, token_hash=_hash(raw), expires_at=_now() + timedelta(days=days)))
            session.commit()
        return raw

    def refresh_user(self, raw: str) -> User | None:
        with Session(self.engine) as session:
            token = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash(raw)))
            if not token or token.revoked_at or _expired(token.expires_at):
                return None
            token.revoked_at = _now()
            user = session.get(User, token.user_id)
            session.commit()
            if user:
                session.refresh(user)
            return user

    def revoke_refresh(self, raw: str) -> None:
        with Session(self.engine) as session:
            token = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash(raw)))
            if token:
                token.revoked_at = _now()
                session.commit()

    def audit(self, user_id: str | None, action: str, resource_type: str, resource_id: str | None = None, metadata: dict | None = None) -> None:
        with Session(self.engine) as session:
            session.add(AuditLog(user_id=user_id, action=action, resource_type=resource_type, resource_id=resource_id, metadata_json=json.dumps(metadata or {})))
            session.commit()

    def usage(self, user_id: str, since: datetime) -> dict[str, Any]:
        with Session(self.engine) as session:
            count = session.scalar(select(func.coalesce(func.sum(UsageLedger.amount), 0)).where(UsageLedger.user_id == user_id, UsageLedger.created_at >= since)) or 0
            cost = session.scalar(select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(UsageLedger.user_id == user_id, UsageLedger.created_at >= since)) or 0
            return {"runs": int(count), "cost_usd": float(cost)}

    def reserve_run(self, user: User, monthly_limit: int, run_id: str | None = None) -> None:
        start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with Session(self.engine) as session:
            count = session.scalar(select(func.coalesce(func.sum(UsageLedger.amount), 0)).where(UsageLedger.user_id == user.id, UsageLedger.created_at >= start)) or 0
            if int(count) >= monthly_limit:
                raise ValueError("Your monthly run quota has been reached.")
            session.add(UsageLedger(user_id=user.id, run_id=run_id, amount=1))
            session.commit()

    def create_api_key(self, user_id: str, name: str) -> str:
        raw = "rp_" + secrets.token_urlsafe(32)
        with Session(self.engine) as session:
            session.add(ApiKey(user_id=user_id, name=name[:100], key_hash=_hash(raw)))
            session.commit()
        return raw

    def create_team(self, user_id: str, name: str) -> Team:
        with Session(self.engine) as session:
            team = Team(name=name[:160], owner_id=user_id)
            session.add(team)
            session.flush()
            session.add(TeamMember(team_id=team.id, user_id=user_id, role="owner"))
            session.commit()
            session.refresh(team)
            return team

    def audit_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
            return [{"id": row.id, "user_id": row.user_id, "action": row.action, "resource_type": row.resource_type, "resource_id": row.resource_id, "metadata": json.loads(row.metadata_json), "created_at": row.created_at.isoformat()} for row in rows]

    def set_plan(self, user_id: str, plan: str, customer_id: str | None = None) -> None:
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            if user:
                user.plan = plan
                if customer_id:
                    user.stripe_customer_id = customer_id
                session.commit()

    def api_key_user(self, raw: str) -> User | None:
        with Session(self.engine) as session:
            key = session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(raw), ApiKey.revoked_at.is_(None)))
            return session.get(User, key.user_id) if key else None

    def as_public(self, user: User) -> dict[str, Any]:
        return {"id": user.id, "email": user.email, "name": user.name, "email_verified": user.email_verified, "plan": user.plan}
