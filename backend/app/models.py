from __future__ import annotations

from sqlalchemy import String, Integer, Float, ForeignKey, JSON, DateTime, func
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Vacancy(Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    jd_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    jd_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    weights_tech: Mapped[float] = mapped_column(Float, default=0.5)
    weights_comm: Mapped[float] = mapped_column(Float, default=0.3)
    weights_cases: Mapped[float] = mapped_column(Float, default=0.2)
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invitations: Mapped[list[Invitation]] = relationship(back_populates="vacancy", cascade="all,delete-orphan")  # type: ignore[name-defined]


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str | None] = mapped_column(String(50))
    tags: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invitations: Mapped[list[Invitation]] = relationship(back_populates="candidate")  # type: ignore[name-defined]


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(16))  # PML/UVL/Self
    token: Mapped[str | None] = mapped_column(String(200))
    code: Mapped[str | None] = mapped_column(String(32))
    ttl: Mapped[int | None] = mapped_column(Integer)
    channel: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(32))
    events: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped[Candidate] = relationship(back_populates="invitations")  # type: ignore[name-defined]
    vacancy: Mapped[Vacancy] = relationship(back_populates="invitations")  # type: ignore[name-defined]


# Самопланировщик: слоты и бронирования
class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    start_at: Mapped[str] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[str] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    vacancy: Mapped[Vacancy] = relationship()  # simple link


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id", ondelete="CASCADE"))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="booked")  # booked|cancelled
    code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

    slot: Mapped[Slot] = relationship()
    candidate: Mapped[Candidate | None] = relationship()


# Live heartbeat для мониторинга активных интервью
class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id", ondelete="SET NULL"))
    lang: Mapped[str] = mapped_column(String(16), default="ru-RU")
    competency: Mapped[str | None] = mapped_column(String(64))
    partial: Mapped[str | None] = mapped_column(String)
    ping_ms: Mapped[int | None] = mapped_column(Integer)
    net: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Пользователь для HR-админки
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="admin")


# Одноразовые инвайт‑токены (JWT‑подобные)
class InviteToken(Base):
    __tablename__ = "invite_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(16))  # pml|uvl|scheduler
    exp: Mapped[str] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# История контактов с кандидатом
class ContactEvent(Base):
    __tablename__ = "contact_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(32))  # pml_issued | call_initiated | slot_changed | note
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Аудит‑лог действий в админке
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Эмбеддинги (pgvector)
try:
    from sqlalchemy import UDT  # type: ignore
except Exception:  # noqa: BLE001
    UDT = object  # type: ignore


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))  # 'cv' | 'jd'
    ref_id: Mapped[int] = mapped_column(Integer)   # candidate_id или vacancy_id
    # хранение как массив float8 (для совместимости без явного типа vector)
    vec: Mapped[list[float]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


# VoIP вызовы и события
class VoipCall(Base):
    __tablename__ = "voip_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))  # voximplant|zadarma|simulated
    external_id: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(16), default="outbound")  # outbound|inbound
    status: Mapped[str] = mapped_column(String(32), default="initiated")  # initiated|ringing|in_progress|finished|failed|cancelled
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id", ondelete="SET NULL"))
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("slots.id", ondelete="SET NULL"))
    phone_from: Mapped[str | None] = mapped_column(String(32))
    phone_to: Mapped[str | None] = mapped_column(String(32))
    dtmf_digits: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[str | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoipEvent(Base):
    __tablename__ = "voip_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("voip_calls.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))  # call.started|dtmf.received|slot.chosen|call.finished|error
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True), server_default=func.now())

