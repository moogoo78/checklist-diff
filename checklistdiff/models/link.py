"""Link — cross-checklist edges that shared name identity cannot express.

Exact cross-checklist matches need no rows here at all: identical names already
collapse to one `Name`, so they are joined structurally. This table exists only
for the two cases that dedup misses — orthographic variants that normalise apart,
and curated *concept* relations, where two checklists use the same name for
genuinely different circumscriptions.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from checklistdiff.models.base import Base, TimestampTZ, utcnow
from checklistdiff.models.enums import LinkRelation, check_in

if TYPE_CHECKING:
    from checklistdiff.models.name import Name


class Link(Base):
    __tablename__ = "link"
    __table_args__ = (
        UniqueConstraint("from_name_id", "to_name_id", "relation"),
        CheckConstraint(check_in("relation", LinkRelation), name="relation"),
        CheckConstraint("from_name_id != to_name_id", name="no_self_link"),
        Index("ix_link_to_name", "to_name_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_name_id: Mapped[int] = mapped_column(
        ForeignKey("name.id", ondelete="CASCADE"), index=True
    )
    to_name_id: Mapped[int] = mapped_column(ForeignKey("name.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(30))

    # "fuzzy:rapidfuzz-token_sort" or "curated" — how the edge came to exist.
    method: Mapped[str] = mapped_column(String(100))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    # Machine-suggested links stay unreviewed until a person accepts them; the
    # UI must show the difference rather than presenting guesses as facts.
    reviewed: Mapped[bool] = mapped_column(default=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, default=utcnow)

    from_name: Mapped["Name"] = relationship(foreign_keys=[from_name_id])
    to_name: Mapped["Name"] = relationship(foreign_keys=[to_name_id])

    def __repr__(self) -> str:
        return f"<Link {self.from_name_id} -{self.relation}-> {self.to_name_id}>"
