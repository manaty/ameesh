# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T1 (#4391): the vocabulary of work items.

The generic core names a work item "tâche" (``CORE``); a module may name it
in its own vocabulary — the dev tooling module declares ``DEV`` ("lot") in
``ameesh.modules.dev``. The core never imports a module implementation (L97):
``active()`` asks the registry for the discovered, always-active modules and
reads their ``VOCABULARY`` attribute. The registry is only read, never
modified (portage decision of mesh-design, #4388).

``CORE`` is the fallback: an empty discovery (bare profile), or modules that
declare no vocabulary, yield it. With the dev module present every project
keeps saying "lot" (canon §5: no project sees "tâche").

The forms carry the French grammar the labels need: singular, plural, gender,
definite and indefinite articles, the contractions ``du``/``de la``/``de l'``
and ``au``/``à la``/``à l'``, and the title forms. A surface renders its
labels through the vocabulary; it never hard-codes the noun.
"""
from __future__ import annotations

from dataclasses import dataclass

#: letters that take the elided article (``l'``, ``de l'``, ``à l'``); a
#: mute ``h`` elides (``l'heure``), an aspirated one declares ``elision=False``
#: (``le héros``). The automatic test covers vowels and ``h``.
VOWELS = "aàâäeéèêëiîïoôöuùûüyÿh"


def _capitalized(word: str) -> str:
    return word[:1].upper() + word[1:]


@dataclass(frozen=True)
class Vocabulary:
    """Grammar of one work item's name (L102 T1, #4391).

    ``gender`` is ``"f"`` or ``"m"``; ``elision`` forces (``True``) or forbids
    (``False``) the elided article, and defaults to the vowel test. The noun
    itself never carries its article: every form is derived here.
    """
    singular: str
    plural: str
    gender: str = "f"
    elision: bool | None = None

    def __post_init__(self):
        for name in ("singular", "plural"):
            word = getattr(self, name)
            if not isinstance(word, str) or not word or word != word.strip():
                raise ValueError("vocabulary %s must be a non-empty word without "
                                 "surrounding spaces" % name)
        if self.gender not in ("f", "m"):
            raise ValueError("vocabulary gender must be 'f' or 'm', not %r"
                             % (self.gender,))
        if self.elision is not None and not isinstance(self.elision, bool):
            raise ValueError("vocabulary elision must be True, False or None")

    @property
    def feminine(self) -> bool:
        return self.gender == "f"

    @property
    def elides(self) -> bool:
        """Whether the definite article elides before the singular noun."""
        if self.elision is not None:
            return self.elision
        return self.singular[:1].lower() in VOWELS

    # -- singular articles -------------------------------------------------
    @property
    def definite(self) -> str:
        """``la tâche``, ``le lot``, ``l'idée``."""
        if self.elides:
            return "l'" + self.singular
        return ("la " if self.feminine else "le ") + self.singular

    @property
    def indefinite(self) -> str:
        """``une tâche``, ``un lot`` (``un``/``une`` never elide)."""
        return ("une " if self.feminine else "un ") + self.singular

    # -- contractions ------------------------------------------------------
    @property
    def of(self) -> str:
        """``de la tâche``, ``du lot``, ``de l'idée`` (``de`` + definite)."""
        if self.elides:
            return "de l'" + self.singular
        return ("de la " if self.feminine else "du ") + self.singular

    @property
    def to(self) -> str:
        """``à la tâche``, ``au lot``, ``à l'idée`` (``à`` + definite)."""
        if self.elides:
            return "à l'" + self.singular
        return ("à la " if self.feminine else "au ") + self.singular

    # -- plural articles ---------------------------------------------------
    @property
    def definite_plural(self) -> str:
        return "les " + self.plural

    @property
    def indefinite_plural(self) -> str:
        return "des " + self.plural

    @property
    def of_plural(self) -> str:
        """``des tâches``, ``des lots`` (``de`` + ``les``)."""
        return "des " + self.plural

    @property
    def to_plural(self) -> str:
        """``aux tâches``, ``aux lots`` (``à`` + ``les``)."""
        return "aux " + self.plural

    # -- title forms -------------------------------------------------------
    @property
    def title(self) -> str:
        """``Tâche``, ``Lot``: the capitalized singular, for a heading."""
        return _capitalized(self.singular)

    @property
    def title_plural(self) -> str:
        """``Tâches``, ``Lots``: the capitalized plural, for a heading."""
        return _capitalized(self.plural)

    # -- rendering ---------------------------------------------------------
    @property
    def noun(self) -> str:
        """The bare singular noun, without its article."""
        return self.singular

    def noun_for(self, number) -> str:
        """The bare noun for a count: singular up to one, plural above."""
        return self.singular if int(number) <= 1 else self.plural

    def count(self, number) -> str:
        """``1 tâche``, ``2 tâches``, ``0 tâche`` (``0`` stays singular)."""
        return "%d %s" % (int(number), self.noun_for(number))

    @property
    def abbreviation(self) -> str:
        """``lot(s)``: the compact singular/plural form of a count."""
        return "%s(s)" % self.singular

    def label(self, number=None, *, capital=False) -> str:
        """``lot #4391`` / ``tâche #4391``; ``capital`` gives ``Lot #4391``.

        The number identifies one work item, so the label stays singular
        whatever its value (``plural`` is the count form, see ``count``).
        """
        noun = _capitalized(self.singular) if capital else self.singular
        return noun if number is None else "%s #%s" % (noun, number)

    def agree(self, masculine: str, feminine: str | None = None, *,
              plural: bool = False) -> str:
        """The adjective in this vocabulary's gender (``stagnant``/``stagnante``)."""
        if self.feminine:
            adjective = feminine if feminine is not None else masculine + "e"
        else:
            adjective = masculine
        if plural and not adjective.endswith("s"):
            adjective += "s"
        return adjective


#: The core's neutral vocabulary: a work item is a "tâche" (feminine).
CORE = Vocabulary("tâche", "tâches", "f")


#: The cached vocabulary of the active modules; ``None`` until first read.
_ACTIVE: Vocabulary | None = None


def _select(modules) -> Vocabulary:
    """The first vocabulary a module declares, else ``CORE``."""
    for module in modules:
        declared = getattr(module, "VOCABULARY", None)
        if declared is None:
            continue
        if not isinstance(declared, Vocabulary):
            raise ValueError("module %r: VOCABULARY must be a Vocabulary"
                             % (getattr(module, "NAME", module),))
        return declared
    return CORE


def _discovered():
    """The discovered modules that are always active, implementations only.

    Read-only: ``registry.discover()`` lists the discovered modules and
    ``registry.get(name)`` returns a built-in implementation, whose
    ``VOCABULARY`` attribute is the declaration. An installed entry point is
    not read here (the registry exposes no implementation for it); the core
    keeps ``CORE`` until a later tranche exposes it.
    """
    from . import modules as modules_pkg
    from .modules import registry
    active = set(modules_pkg.always_active())
    try:
        discovered = registry.discover().modules
    except Exception:  # noqa: BLE001 - a broken discovery must not break a label
        return []
    out = []
    for row in discovered:
        if row.name not in active:
            continue
        module = registry.get(row.name)
        if module is not None:
            out.append(module)
    return out


def active(modules=None) -> Vocabulary:
    """The vocabulary of the active module, ``CORE`` when none declares one.

    ``modules`` is the discovery seam for tests: an iterable of module
    implementations. Without it the discovered, always-active modules are
    read once and the result is cached; ``reset()`` forgets it.
    """
    global _ACTIVE
    if modules is not None:
        return _select(modules)
    if _ACTIVE is None:
        _ACTIVE = _select(_discovered())
    return _ACTIVE


def reset() -> None:
    """Forget the cached active vocabulary (tests, a module activation change)."""
    global _ACTIVE
    _ACTIVE = None
