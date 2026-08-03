"""Checklist and Release — the provenance layer."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from checklistdiff.models.base import Base, TimestampTZ, utcnow
from checklistdiff.models.enums import (
    IdStability,
    ReleaseStatus,
    SourceFormat,
    check_in,
)

if TYPE_CHECKING:
    from checklistdiff.models.usage import Track, Usage


class Checklist(Base):
    """A published name list that issues successive releases."""

    __tablename__ = "checklist"
    __table_args__ = (
        CheckConstraint(check_in("id_stability", IdStability), name="id_stability"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(500))
    homepage: Mapped[str | None] = mapped_column(String(1000))
    license: Mapped[str | None] = mapped_column(String(200))

    # Set from evidence, via `ckdiff checklist check-ids`. The default is the
    # pessimistic one: assume IDs shuffle until a comparison proves otherwise,
    # because trusting unstable IDs produces a diff of pure noise.
    id_stability: Mapped[str] = mapped_column(
        String(20), default=IdStability.UNSTABLE.value
    )

    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, default=utcnow)

    releases: Mapped[list["Release"]] = relationship(
        back_populates="checklist",
        order_by="Release.released_on",
        cascade="all, delete-orphan",
    )
    tracks: Mapped[list["Track"]] = relationship(
        back_populates="checklist", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Checklist {self.code!r} releases={len(self.releases)}>"


class Release(Base):
    """One published version of a checklist. Immutable once `ready`."""

    __tablename__ = "release"
    __table_args__ = (
        UniqueConstraint("checklist_id", "version"),
        CheckConstraint(check_in("format", SourceFormat), name="format"),
        CheckConstraint(check_in("status", ReleaseStatus), name="status"),
        Index("ix_release_checklist_ordered", "checklist_id", "released_on"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("checklist.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(100))
    released_on: Mapped[date | None] = mapped_column(Date)
    ingested_at: Mapped[datetime] = mapped_column(TimestampTZ, default=utcnow)

    source_uri: Mapped[str | None] = mapped_column(String(1000))
    # Guards re-ingest: the same bytes under the same version is a no-op, and
    # different bytes under an already-ingested version is an error worth raising.
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(20))

    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(20), default=ReleaseStatus.IMPORTING.value
    )
    notes: Mapped[str | None] = mapped_column(Text)

    checklist: Mapped["Checklist"] = relationship(back_populates="releases")
    usages: Mapped[list["Usage"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Release {self.version!r} usages={self.usage_count}>"
