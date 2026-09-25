"""Live-only participant names: speakable commedia dell'arte masks for 12-hex ids.
Names are recycled after death and never persisted; the id is stable while the row is retained
(GC-bounded), so use the id for anything spanning time or destructive.
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
    """Return a random mask not in *taken* (case-insensitive), suffixing ``-2``, ``-3``... when
    full.
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
    return _NAME_RE.fullmatch(name) is not None
