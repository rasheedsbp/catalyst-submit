"""Subject terms taken from the journals, not guessed from the title.

A phrase pulled out of a title is a guess that some journal indexes it. The
categories journals are actually classified under are not a guess: SCImago
assigns every journal one or more subject categories, so a category name is
a term with a known, countable set of journals behind it. Matching a title
against that vocabulary cannot produce a term that finds nothing.

This does not replace the OpenAlex terms — those retrieve the individual
journals that publish on a topic, which is finer-grained than a category
covering hundreds of titles. It backs them up: when the title's own phrases
retrieve nothing, a category match still lands the search in the right
neighbourhood, and every category offered can be shown with the number of
journals it holds.

The vocabulary is derived from whatever SCImago file is in data/, so it
reflects the database being searched rather than a hardcoded list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that carry no discriminating power inside a category name. Without
# this, "Building and Construction" matches any title containing "and".
CATEGORY_STOPWORDS = {
    "and", "of", "the", "in", "for", "a", "an", "general", "miscellaneous",
    "all", "other", "applied", "theoretical", "various",
}


@dataclass
class SubjectMatch:
    name: str
    journals: int
    score: float
    matched: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.name} ({self.journals:,} journals)"


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]


def _content(name: str) -> list[str]:
    return [w for w in _words(name) if w not in CATEGORY_STOPWORDS]


@dataclass
class SubjectVocabulary:
    """Every subject category in the journal database, with journal counts."""

    categories: dict[str, int] = field(default_factory=dict)
    areas: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_scimago(cls, index=None) -> "SubjectVocabulary":
        if index is None:
            from .scimago import ScimagoIndex

            index = ScimagoIndex()

        cats: dict[str, int] = {}
        areas: dict[str, int] = {}
        seen: set[int] = set()

        for entry in getattr(index, "by_issn", {}).values():
            # by_issn holds the same entry under print and electronic ISSNs;
            # count each journal once.
            if id(entry) in seen:
                continue
            seen.add(id(entry))

            for name in (entry.get("sjr_categories") or {}):
                cats[name] = cats.get(name, 0) + 1

            raw = entry.get("sjr_areas")
            if raw:
                for part in str(raw).split(";"):
                    a = part.strip().lower()
                    if a and a != "nan":
                        areas[a] = areas.get(a, 0) + 1

        return cls(categories=cats, areas=areas)

    @property
    def available(self) -> bool:
        return bool(self.categories)

    def match(self, title: str, extra_terms: list[str] | None = None,
              limit: int = 5, min_journals: int = 5) -> list[SubjectMatch]:
        """Categories whose name overlaps the title, best first.

        Scoring is deliberately conservative. A category earns a point per
        distinctive word it shares with the title, weighted by how much of
        the CATEGORY that word covers — matching both words of "Thermal
        Comfort" is worth far more than matching "engineering" alone, which
        would otherwise drag in half the vocabulary. Whole-name containment
        is treated as certain.
        """
        haystack = " ".join([title.lower()] + [t.lower() for t in extra_terms or []])
        hay_words = set(_words(haystack))
        if not hay_words:
            return []

        out: list[SubjectMatch] = []
        for name, count in self.categories.items():
            if count < min_journals:
                continue

            parts = _content(name)
            if not parts:
                continue

            hits = [p for p in parts if p in hay_words]
            if not hits:
                continue

            coverage = len(hits) / len(parts)

            # One generic word in common is not a subject match. Require
            # either the whole category name, or most of it.
            if name.lower() in haystack:
                score = 1.0
            elif coverage >= 0.5:
                score = coverage * 0.8
            else:
                continue

            out.append(SubjectMatch(name, count, score, hits))

        out.sort(key=lambda m: (-m.score, -m.journals))
        return out[:limit]


def categories_of(issns: list[str], index=None, limit: int = 5,
                  min_share: float = 0.30) -> list[SubjectMatch]:
    """The subject categories a set of journals is actually classified under.

    This is the reliable direction. Matching a title's words against category
    names barely works — "Building and Construction" shares no word with
    "aged reclaimed asphalt pavement", and "learning" in a deep-learning
    title matches "E-Learning". Journals do not have that problem: find the
    journals a phrase retrieves, look up what SCImago files them under, and
    the dominant category is the subject, established from the database
    rather than from string overlap.

    Categories are returned with the share of the sample that carries them,
    and must cover at least a third of it. A journal or two filed under
    something unrelated is normal in any sample and must not be allowed to
    name the subject.
    """
    if index is None:
        from .scimago import ScimagoIndex

        index = ScimagoIndex()

    counts: dict[str, int] = {}
    matched = 0
    for issn in issns:
        entry = index.lookup([issn]) if hasattr(index, "lookup") else None
        cats = (entry or {}).get("sjr_categories") or {}
        if not cats:
            continue
        matched += 1
        for name in cats:
            counts[name] = counts.get(name, 0) + 1

    if not matched:
        return []

    vocab = vocabulary(index)
    out = [
        SubjectMatch(name, vocab.categories.get(name, n), n / matched)
        for name, n in counts.items()
        if n / matched >= min_share
    ]
    out.sort(key=lambda m: -m.score)
    return out[:limit]


_VOCAB: SubjectVocabulary | None = None


def vocabulary(index=None) -> SubjectVocabulary:
    """Cached vocabulary — parsing SCImago is not cheap."""
    global _VOCAB
    if _VOCAB is None or index is not None:
        built = SubjectVocabulary.from_scimago(index)
        if index is None:
            _VOCAB = built
        return built if index is not None else _VOCAB
    return _VOCAB


def subject_terms(title: str, extra_terms: list[str] | None = None,
                  limit: int = 3) -> list[SubjectMatch]:
    vocab = vocabulary()
    if not vocab.available:
        return []
    return vocab.match(title, extra_terms, limit=limit)
