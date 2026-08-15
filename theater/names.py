"""Live-only participant names.

Every participant has a 12-hex-char id that a human cannot say out loud. A
random commedia dell'arte mask gives it a name the human can speak and type, so
``tell Arlequin to run the tests'' works and so does ``theater kill Arlequin``.

Names are recyclable aliases held only by live participants. When a participant
dies its name is released — a later participant may pick up the same mask, so
a name that appears in a user's scrollback can point at a *different* agent
after a death and respawn. The id is the stable identity for as long as the
row is retained: it outlives death and daemon restarts, but dead rows are
eventually deleted by retention GC, so historical access is retention-bounded.
Use the id — not the name — for any targeting that spans time or that has
destructive consequences, because a recycled name can identify a successor.

The name is never persisted: it lives in the Registry's in-memory map and is
regenerated when the daemon restarts. Dead rows have ``name = None``, so a
dead participant shows as ``-`` in the CLI and cannot be reached by name. See
``theater/daemon/registry.py``.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable

MASKS: tuple[str, ...] = (
    "Arlequin",
    "Arlecchino",
    "Pierrot",
    "Pedrolino",
    "Colombine",
    "Scaramouche",
    "Brighella",
    "Truffaldino",
    "Scapino",
    "Mezzetino",
    "Pulcinella",
    "Polichinelle",
    "Pantalone",
    "Dottore",
    "Balanzone",
    "Graziano",
    "Capitano",
    "Matamore",
    "Fracasse",
    "Rodomonte",
    "Tartaglia",
    "Coviello",
    "Zanni",
    "Burattino",
    "Trivelin",
    "Sganarelle",
    "Scapin",
    "Mascarille",
    "Crispin",
    "Gilles",
    "Cassandre",
    "Leandre",
    "Toinette",
    "Dorine",
    "Smeraldine",
    "Franceschine",
    "Isabelle",
    "Rosaure",
    "Flaminia",
    "Ottavio",
    "Florindo",
    "Silvio",
    "Lelio",
    "Stenterello",
    "Meneghino",
    "Gianduja",
    "Rugantino",
    "Tabarin",
    "Turlupin",
    "Jodelet",
    "Beltrame",
    "Sbrigani",
    "Nerine",
    "Zerbinette",
    "Angelique",
    "Lucinde",
    "Valerio",
    "Clarice",
    "Pasquariello",
    "Facanapa",
    "Bortolo",
    "Giacometta",
    "Mirandolina",
    "Giacinto",
    "Pasquina",
    "Corallina",
    "Spinetta",
    "Doralba",
    "Fabrizio",
    "Ferramondo",
    "Alidoro",
    "Altobello",
    "Cintio",
    "Sosie",
    "Maitre",
    "Harpagon",
    "Elvire",
    "Dorimele",
    "Marphurius",
    "Beralde",
    "Damis",
    "Valere",
    "Launce",
    "Gobbo",
    "Truffa",
    "Buralicchio",
    "Celio",
    "Fulgenzio",
    "Rosaura",
    "Belpiano",
    "Cannocchia",
    "Formicone",
    "Gnorro",
    "Puccio",
    "Simone",
    "Cicogna",
    "Giocondo",
    "Malianno",
    "Morbetto",
    "Calabrese",
)

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")


def pick(taken: Iterable[str]) -> str:
    """Return a random mask not in *taken*, compared case-insensitively.

    If every mask is taken, appends a numeric suffix (``-2``, ``-3``, ...)
    until a free name is found.  Never raises and never returns a name
    already in *taken*.
    """
    taken_cf = {t.casefold() for t in taken}
    pool = [m for m in MASKS if m.casefold() not in taken_cf]
    if pool:
        return random.choice(pool)
    base = random.choice(MASKS)
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if candidate.casefold() not in taken_cf:
            return candidate
        n += 1


def is_valid_name(name: str) -> bool:
    """Whether *name* satisfies the rename format rules."""
    return _NAME_RE.match(name) is not None
