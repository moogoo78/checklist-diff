"""Name — the nomenclatural layer.

One row per distinct scientific name in the *entire* database, deduped on
`norm_key`. Every checklist's usages point here, which is what turns "who else
has this name, and what do they say about it?" into a single join on `name_id`.

Because that dedup is load-bearing, `norm_key` derivation (naming/normalize.py)
is the most safety-critical code in the project: too loose and two real names
merge into one, too strict and one name fragments across releases, corrupting the
change history under name-anchored tracking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from checklistdiff.models.base import Base
from checklistdiff.models.enums import NomenclaturalCode, check_in

if TYPE_CHECKING:
    from checklistdiff.models.usage import Usage

# Kept in sync with the FTS5 virtual table created in the initial migration.
FTS_TABLE = "name_fts"


class Name(Base):
    __tablename__ = "name"
    __table_args__ = (
        CheckConstraint(
            check_in("nomenclatural_code", NomenclaturalCode), name="nom_code"
        ),
        Index("ix_name_canonical", "canonical_name"),
        Index("ix_name_genus_epithet", "genus", "specific_epithet"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # As it appeared in the source, verbatim.
    scientific_name: Mapped[str] = mapped_column(String(1000))

    # Parsed components, from gnparser.
    canonical_name: Mapped[str] = mapped_column(String(500))
    authorship: Mapped[str | None] = mapped_column(String(500))
    rank: Mapped[str | None] = mapped_column(String(50), index=True)
    rank_marker: Mapped[str | None] = mapped_column(String(20))
    genus: Mapped[str | None] = mapped_column(String(200))
    specific_epithet: Mapped[str | None] = mapped_column(String(200))
    infraspecific_epithet: Mapped[str | None] = mapped_column(String(200))
    nomenclatural_code: Mapped[str] = mapped_column(
        String(20), default=NomenclaturalCode.UNKNOWN.value
    )

    # Identity of this row: canonical + authorship. Authorship is included so
    # homonyms stay distinct (Aotus Endl. the plant vs Aotus Illiger the monkey).
    norm_key: Mapped[str] = mapped_column(String(600), unique=True)

    # Canonical name alone, ignoring authorship. Groups homonyms, and anchors
    # tracks when a checklist's own IDs are untrustworthy -- so that adding or
    # correcting an author string reports `author_changed` rather than
    # fragmenting the taxon's history. Not unique. See naming/normalize.py.
    canonical_key: Mapped[str] = mapped_column(String(500), index=True)

    # Full gnparser output, retained so a normalisation change can be re-derived
    # without re-parsing every name.
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parse_quality: Mapped[int | None] = mapped_column(Integer)
    parse_warnings: Mapped[str | None] = mapped_column(Text)

    usages: Mapped[list["Usage"]] = relationship(back_populates="name")

    def __repr__(self) -> str:
        return f"<Name {self.scientific_name!r}>"
