"""Change — the computed diff between two consecutive releases.

Derived data, never authoritative: `ckdiff diff run --force` drops and rebuilds
every row for a release pair. That is deliberate, so improving the diff algorithm
never requires re-ingesting a single source file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from checklistdiff.models.base import Base
from checklistdiff.models.enums import ChangeType, check_in

if TYPE_CHECKING:
    from checklistdiff.models.usage import Track, Usage


class Change(Base):
    __tablename__ = "change"
    __table_args__ = (
        CheckConstraint(check_in("type", ChangeType), name="type"),
        Index("ix_change_pair", "from_release_id", "to_release_id"),
        Index("ix_change_pair_type", "from_release_id", "to_release_id", "type"),
        Index("ix_change_track", "track_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("checklist.id", ondelete="CASCADE"), index=True
    )
    from_release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE")
    )
    to_release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE")
    )

    # Null only for set-level changes that span several tracks (lumped/split),
    # where the participants live in `detail` instead.
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("track.id", ondelete="CASCADE")
    )

    type: Mapped[str] = mapped_column(String(30), index=True)

    # Null on `added` (nothing before) and on `removed` (nothing after).
    from_usage_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage.id", ondelete="CASCADE")
    )
    to_usage_id: Mapped[int | None] = mapped_column(
        ForeignKey("usage.id", ondelete="CASCADE")
    )

    # Before/after values, and for pass-3 types the participating usages.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Set only for inferred changes (probable_rename); null when asserted.
    confidence: Mapped[float | None] = mapped_column(Float)

    track: Mapped["Track | None"] = relationship()
    from_usage: Mapped["Usage | None"] = relationship(foreign_keys=[from_usage_id])
    to_usage: Mapped["Usage | None"] = relationship(foreign_keys=[to_usage_id])

    def __repr__(self) -> str:
        return f"<Change {self.type} {self.from_usage_id}->{self.to_usage_id}>"
