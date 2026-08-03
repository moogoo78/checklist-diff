# Synthetic fixture

A hand-built checklist in two releases, constructed so that **every change type
occurs at least once**. This is the executable specification of the diff
semantics — read this table before changing `checklistdiff/diff/`.

Genus *Testia* is invented, so nothing here can be confused with a real name.

## The taxa

| ID | v1 (2023.1) | v2 (2024.1) | Expected change | Notes |
| --- | --- | --- | --- | --- |
| T001 | *Testia alpha* Smith — accepted | *Testia alpha* Smith — accepted | *(none)* | Control: must produce no change row. |
| T002 | *Testia beta* Smith — accepted | *Testia beta* Jones — accepted | `author_changed` | Same canonical name, corrected author. |
| T003 | *Testia gamma* Smith — accepted | *Testia gammma* Smith — accepted | `renamed` under ID anchoring; `probable_rename` under name anchoring | Orthographic fix. |
| T004 | *Testia delta* Smith — accepted | *Testia delta* Smith — **synonym of T005** | `status_changed` + `accepted_changed` | Sunk into synonymy. |
| T005 | *Testia epsilon* Smith — accepted | *Testia epsilon* Smith — accepted | *(receives the lump)* | |
| T006 | *Testia zeta* Smith — synonym of T005 | *Testia zeta* Smith — synonym of **T007** | `accepted_changed` | Synonym repointed. |
| T007 | *Testia eta* Smith — accepted | *Testia eta* Smith — accepted | *(target of T006)* | |
| T008 | *Testia theta* Smith — accepted, parent *Testia* | *Testia theta* Smith — accepted, parent **Testiella** | `reclassified` | Moved genus. |
| T009 | *Testia iota* Smith — accepted, rank species | *Testia iota* Smith — accepted, rank **subspecies** | `rank_changed` | |
| T010 | *Testia kappa* Smith — accepted | *(absent)* | `removed` | Genuinely dropped. |
| T011 | *(absent)* | *Testia lambda* Smith — accepted | `added` | Newly described. |
| T012 | *Testia mu* Smith — accepted, id T012 | *Testia mu* Smith — accepted, id **T912** | `id_replaced` under ID anchoring; *(none)* under name anchoring | Same name, renumbered. |

## Set-level cases

**Lump (2 → 1).** T004 and T013 are both accepted in v1; in v2 both are synonyms
of T005. Expect one `lumped` change naming both sources and T005 as the target.

| ID | v1 | v2 |
| --- | --- | --- |
| T013 | *Testia nu* Smith — accepted | *Testia nu* Smith — synonym of T005 |

**Split (1 → 3).** T014 is accepted in v1 with two synonyms (T015, T016). In v2
all three are accepted. Expect one `split` change with T014 as source and all
three as products.

| ID | v1 | v2 |
| --- | --- | --- |
| T014 | *Testia xi* Smith — accepted | *Testia xi* Smith — accepted |
| T015 | *Testia omicron* Smith — synonym of T014 | *Testia omicron* Smith — **accepted** |
| T016 | *Testia pi* Smith — synonym of T014 | *Testia pi* Smith — **accepted** |

## Why it runs twice

`test_diff.py` runs this fixture under both anchoring modes, because they must
each produce a defensible — and *different* — result:

- **`persistent`** anchors on the publisher's ID. Detects `renamed` (T003) and
  `id_replaced` (T012).
- **`unstable`** anchors on the canonical name. Cannot detect `renamed` by
  construction, so T003 surfaces as an advisory `probable_rename`; T012 produces
  no change at all, because the renumbering is invisible when names are the
  anchor.

A change to the engine that makes both modes agree is a bug, not an improvement.
