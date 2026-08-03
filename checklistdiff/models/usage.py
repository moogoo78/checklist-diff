"""Usage, Track, UsageTrack — the assertion layer and its threading over time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from checklistdiff.models.base import Base
from checklistdiff.models.enums import AnchorKind, TaxonomicStatus, check_in

if TYPE_CHECKING:
    from checklistdiff.models.checklist import Checklist, Release
    from checklistdiff.models.name import Name


class Usage(Base):
    """What one release says about one name.

    Append-only. Nothing here is ever updated after ingest completes — that
    immutability is the entire basis for trusting the reconstructed history.
    """

    __tablename__ = "usage"
    __table_args__ = (
        # source_taxon_id is nullable (some checklists have no IDs at all), and
        # SQLite treats NULLs as distinct, so this constrains only real IDs.
        UniqueConstraint("release_id", "source_taxon_id"),
        CheckConstraint(check_in("status", TaxonomicStatus), name="status"),
        Index("ix_usage_release_name", "release_id", "name_id"),
        Index("ix_usage_release_status", "release_id", "status"),
        Index("ix_usage_accepted", "accepted_usage_id"),
        Index("ix_usage_parent", "parent_usage_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), index=True
    )
    source_taxon_id: Mapped[str | None] = mapped_column(String(200))
    name_id: Mapped[int] = mapped_column(ForeignKey("name.id"), index=True)

    status: Mapped[str] = mapped_column(
        String(30), default=TaxonomicStatus.UNKNOWN.value
    )

    # Both self-FKs resolve within the same release, always.
    accepted_usage_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage.id", ondelete="SET NULL")
    )
    parent_usage_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage.id", ondelete="SET NULL")
    )

    rank: Mapped[str | None] = mapped_column(String(50), index=True)

    # Denormalised higher classification (kingdom..genus) so listing a result set
    # never needs a recursive walk up parent_usage_id.
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    vernacular: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)

    # The untouched source row, for auditing a surprising diff against what the
    # publisher actually shipped.
    source_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    release: Mapped["Release"] = relationship(back_populates="usages")
    name: Mapped["Name"] = relationship(back_populates="usages")
    accepted: Mapped["Usage | None"] = relationship(
        remote_side=[id], foreign_keys=[accepted_usage_id]
    )
    parent: Mapped["Usage | None"] = relationship(
        remote_side=[id], foreign_keys=[parent_usage_id]
    )

    def __repr__(self) -> str:
        return f"<Usage id={self.id} name_id={self.name_id} {self.status}>"


class Track(Base):
    """The thread joining "the same taxon" across releases of one checklist.

    Materialised rather than derived from the change log: reconstructing a
    taxon's history by walking change rows is a recursive query, whereas this
    makes it one indexed lookup.

    `anchor_kind` records *how* the thread was established, so a reader can tell
    a track built on stable publisher IDs from one stitched together by name —
    the two carry very different confidence.
    """

    __tablename__ = "track"
    __table_args__ = (
        UniqueConstraint("checklist_id", "anchor_kind", "anchor_key"),
        CheckConstraint(check_in("anchor_kind", AnchorKind), name="anchor_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("checklist.id", ondelete="CASCADE"), index=True
    )
    anchor_kind: Mapped[str] = mapped_column(String(20))
    # The source_taxon_id, or the name's norm_key, depending on anchor_kind.
    anchor_key: Mapped[str] = mapped_column(String(600))

    checklist: Mapped["Checklist"] = relationship(back_populates="tracks")
    memberships: Mapped[list["UsageTrack"]] = relationship(
        back_populates="track",
        cascade="all, delete-orphan",
        order_by="UsageTrack.release_id",
    )

    def __repr__(self) -> str:
        return f"<Track {self.anchor_kind}:{self.anchor_key!r}>"


class UsageTrack(Base):
    """Membership of a usage in a track. One track per usage."""

    __tablename__ = "usage_track"
    __table_args__ = (
        # A track may hold at most one usage per release; two would mean the
        # anchor is ambiguous, which the matcher must resolve before writing.
        UniqueConstraint("track_id", "release_id"),
    )

    usage_id: Mapped[int] = mapped_column(
        ForeignKey("usage.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("track.id", ondelete="CASCADE"), index=True
    )
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), index=True
    )

    track: Mapped["Track"] = relationship(back_populates="memberships")
    usage: Mapped["Usage"] = relationship()
