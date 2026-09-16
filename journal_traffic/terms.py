"""Turn a paper title into search terms that actually retrieve journals.

Deliberately not an LLM. The question is not "what is this paper about" — it
is "which phrases retrieve the right journals from OpenAlex". OpenAlex already
knows that, because it has indexed the papers. So the approach is:

  1. Pull candidate phrases out of the title (noun-ish n-grams, stopwords and
     methodology filler removed).
  2. Ask OpenAlex which topics its own index assigns to papers matching the
     title. These are curated labels, better than anything guessed from the
     string alone.
  3. Score each candidate by how many recent papers it retrieves — a phrase
     that finds nothing is useless however well it describes the paper.

A title usually spans several fields (net-zero + buildings + digital twins),
so this returns a ranked LIST of terms to run separately, not one term. That
matches how the tool works: separate runs, then look for journals appearing
in more than one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that never help a subject search. Methodology and hedging words are
# the big ones: "a novel approach for" retrieves everything and nothing.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "for", "with",
    "to", "from", "by", "at", "as", "is", "are", "was", "were", "be", "been",
    "using", "based", "via", "toward", "towards", "through", "into", "under",
    "over", "between", "among", "during", "within", "across",
    "novel", "new", "improved", "enhanced", "efficient", "effective",
    "robust", "optimal", "optimized", "advanced", "intelligent", "smart",
    "approach", "method", "methods", "methodology", "technique", "techniques",
    "framework", "model", "models", "modelling", "modeling", "system",
    "systems", "study", "studies", "analysis", "investigation", "review",
    "paper", "article", "research", "case", "application", "applications",
    "performance", "evaluation", "assessment", "comparison", "comparative",
    "design", "development", "implementation", "proposed", "hybrid",
    "its", "their", "this", "that", "these", "those", "we", "our",
}

# Domain phrases worth preserving whole — splitting them destroys the meaning.
KNOWN_PHRASES = [
    "net zero energy building", "nearly zero energy building",
    "net zero emission", "digital twin", "building information modelling",
    "building information modeling", "life cycle assessment",
    "wireless sensor network", "internet of things", "machine learning",
    "deep learning", "reinforcement learning", "federated learning",
    "renewable energy", "energy efficiency", "demand response",
    "indoor air quality", "thermal comfort", "smart grid", "microgrid",
    "electric vehicle", "supply chain", "compressive sensing",
    "intrusion detection", "computer vision", "natural language processing",
    "predictive maintenance", "structural health monitoring",
    "circular economy", "carbon footprint", "occupant behaviour",
    "occupant behavior", "energy management", "fault detection",
]


@dataclass
class Term:
    text: str
    source: str          # "topic" | "phrase" | "ngram"
    works: int = 0       # recent papers retrieved — the usefulness signal
    note: str = ""       # why it survived when the normal bar was not met

    def __str__(self) -> str:
        tail = f", {self.note}" if self.note else ""
        unit = "journals" if self.source == "category" else "papers"
        return f"{self.text}  ({self.works:,} {unit}, from {self.source}{tail})"


def _clean(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s-]", " ", t)   # drop punctuation, keep hyphens
    return re.sub(r"\s+", " ", t).strip()


def coined_tokens(title: str) -> set[str]:
    """Acronyms and invented names, which retrieve nothing as search terms.

    A method the authors named themselves — DRMLC, PA-ACO, Wetbond-REX — is
    unique to the paper by construction, so searching it finds the paper and
    no journals. Detected from the ORIGINAL casing, before lowercasing throws
    the evidence away: an all-caps run, or a token mixing caps with digits or
    hyphens, is a coined name rather than a subject.
    """
    # A title typed in BLOCK CAPITALS has no case information to read, and
    # treating every word in it as an acronym leaves nothing to search. If
    # most of the title trips the test, the test is measuring the typing
    # convention rather than the words.
    words = [w for w in re.findall(r"[A-Za-z-]+", title) if len(w) > 1]
    if words and sum(w.isupper() for w in words) / len(words) > 0.5:
        return set()

    out: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9-]+", title):
        core = raw.strip("-")
        if len(core) < 2:
            continue
        letters = [c for c in core if c.isalpha()]
        if not letters:
            continue
        caps = sum(c.isupper() for c in letters) / len(letters)
        if caps > 0.6 or (caps > 0.3 and any(c.isdigit() for c in core)):
            out.add(core.lower())
            out.update(part.lower() for part in core.split("-") if len(part) > 1)
            continue

        # "Wetbond-REX": neither half is coined by ratio, but one half is an
        # acronym, which makes the compound a product name all the same.
        parts = [p for p in core.split("-") if p]
        if len(parts) > 1 and any(
            p.isupper() and len(p) > 1 for p in parts
        ):
            out.add(core.lower())
            out.update(p.lower() for p in parts if len(p) > 1)
    return out


def content_chunks(title: str, drop: set[str] | None = None) -> list[list[str]]:
    """Runs of content words, split wherever a function word interrupts.

    This is what stops nonsense candidates. Sliding an n-gram window over a
    title with the stopwords deleted joins words that were never adjacent:
    "Social Media Marketing on Buying Behaviour of Durable Goods" collapses
    to one word sequence and yields "marketing buying behaviour", which is
    not a subject anyone indexes. Splitting at the removed words keeps
    "social media marketing", "buying behaviour" and "durable goods" apart,
    which is how a reader would parse it too.
    """
    drop = drop or set()
    chunks: list[list[str]] = []
    current: list[str] = []

    # Punctuation is a hard boundary; a colon usually separates a coined
    # method name from the actual subject.
    for segment in re.split(r"[:;,()\[\]/–—]|\.\s", title):
        current = []
        for word in _clean(segment).split():
            # Two-letter words are noise, but two-character tokens carrying a
            # digit are subjects: 5G, 6G, 3D, AI is three... keep anything
            # with a digit in it.
            too_short = len(word) <= 2 and word.isalpha()
            if word in STOPWORDS or word in drop or too_short:
                if current:
                    chunks.append(current)
                    current = []
                continue
            current.append(word)
        if current:
            chunks.append(current)

    return chunks


def candidate_phrases(title: str, max_terms: int = 14) -> list[str]:
    """Phrases from the title itself, longest and most specific first.

    Trigrams matter more than they look. A subject like "reclaimed asphalt
    pavement" or "wireless body area network" is three words, and generating
    only bigrams cut it into halves that retrieve either far too much
    ("asphalt pavement") or nothing at all ("reclaimed asphalt") — which is
    why niche titles were coming back with no usable terms.
    """
    text = _clean(title)
    found: list[str] = []

    # Known domain phrases first — these are the ones worth keeping intact.
    for phrase in KNOWN_PHRASES:
        if phrase in text:
            found.append(phrase)
            # A comma, not a space: removing "reinforcement learning" from
            # the middle of a title must leave a boundary behind, or the
            # words either side become falsely adjacent and produce a
            # phrase the author never wrote.
            text = text.replace(phrase, " , ")

    # Coined names are demoted, not deleted. An established acronym (IoT, 5G,
    # CSI, EEG) is a real subject with a real literature, and a made-up one
    # (DRMLC, PA-ACO) retrieves almost nothing — the volume check already
    # separates those two better than any capitalisation rule can. Dropping
    # them outright was discarding valid subjects, so they stay in the text,
    # keep the adjacency of the words around them, and simply rank last.
    coined = coined_tokens(title)
    chunks = content_chunks(text)

    # A whole chunk is the best guess at a subject: it is the phrase the
    # author actually wrote, bounded by the words they used to join clauses.
    for chunk in chunks:
        if 2 <= len(chunk) <= 4:
            found.append(" ".join(chunk))

    # Then windows INSIDE a chunk — never across a boundary.
    for size in (3, 2):
        for chunk in chunks:
            for i in range(len(chunk) - size + 1):
                found.append(" ".join(chunk[i:i + size]))

    for chunk in chunks:
        found.extend(chunk)

    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)

    # Anything built only from coined tokens goes to the back of the queue,
    # so the request budget is spent on real subjects first.
    def coined_only(phrase: str) -> bool:
        parts = phrase.split()
        return bool(parts) and all(p in coined for p in parts)

    out.sort(key=coined_only)
    return out[:max_terms]


def topics_for_title(client, title: str, limit: int = 4) -> list[str]:
    """Ask OpenAlex which topics it assigns to papers matching this title.

    Curated labels beat guessed phrases, because they are what OpenAlex
    actually uses to organise the literature.
    """
    try:
        page = client._get(
            "/works",
            search=title,
            per_page=25,
            select="id,topics",
        )
    except Exception:  # noqa: BLE001
        return []

    counts: dict[str, int] = {}
    for w in page.get("results", []):
        for t in (w.get("topics") or [])[:2]:   # primary topics only
            name = t.get("display_name")
            if name:
                counts[name] = counts.get(name, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [name for name, _ in ranked[:limit]]


MAX_MEASURED = 12       # candidates worth spending a request on
WORKERS = 6             # parallel count lookups


def measure(client, terms: list[Term]) -> tuple[list[Term], bool]:
    """Attach a recent-paper count to each candidate, in parallel.

    One count is one API call, and doing fifteen of them one after another —
    each with a courtesy pause and up to four retries — is what made term
    detection feel like it had hung. The calls are independent, so they run
    on a small thread pool instead. Each worker gets its own client, because
    a requests.Session is not safe to share across threads.

    Returns the terms and whether the API answered at all: if every call
    failed, a count of zero means "we could not check", not "nothing is
    published on this", and the caller must not treat the two the same.
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date

    terms = terms[:MAX_MEASURED]
    y_from = date.today().year - 5
    answered = False

    def count(term: Term) -> tuple[Term, int, bool]:
        worker = type(client)(
            mailto=getattr(client, "mailto", None),
            pause=0.0,                      # the pool already spaces requests
            use_cache=getattr(client, "use_cache", True),
        )
        try:
            page = worker._get(
                "/works",
                filter=f"title_and_abstract.search:{term.text},"
                       f"type:article,publication_year:>{y_from}",
                per_page=1,
            )
            return term, page.get("meta", {}).get("count", 0), True
        except Exception:  # noqa: BLE001
            return term, 0, False

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for term, works, ok in pool.map(count, terms):
            term.works = works
            answered = answered or ok

    return terms, answered


def score_terms(client, terms: list[Term], min_works: int = 200) -> list[Term]:
    """Rank candidates, relaxing the bar rather than returning nothing.

    The original single threshold was the reason most titles came back with
    no terms. A specialised subject legitimately has a few hundred recent
    papers, and a hyphenated or acronym-heavy title produces candidates that
    are individually rare even though the paper sits in a real field. So the
    bar drops in stages, and a curated OpenAlex topic label is exempt from
    the lower bound entirely — those are retrieval labels the index itself
    assigns, not phrases guessed from a string.
    """
    terms, answered = measure(client, terms)

    if not answered:
        # The index never replied. Fall back to the title's own phrases,
        # marked so the caller can say the counts are unknown.
        for t in terms:
            t.note = "unverified — OpenAlex unreachable"
        return terms[:6]

    def keep(bar: int, ceiling: int = 400_000) -> list[Term]:
        return [t for t in terms
                if (t.source == "topic" and t.works > 0)
                or (bar <= t.works <= ceiling)]

    # Too few papers means no journal specialises in it; too many means the
    # term is a whole discipline and returns megajournals.
    scored = keep(min_works)

    for bar, note in ((50, "narrow field"), (10, "very narrow field")):
        if scored:
            break
        scored = keep(bar)
        for t in scored:
            if t.works < min_works:
                t.note = note

    if not scored:
        # Last resort: a broad term beats no term at all, because the run
        # can still be narrowed by quartile and subject matching afterwards.
        scored = [t for t in terms if t.works > 0]
        for t in scored:
            t.note = "broad — results may include megajournals"

    scored.sort(key=lambda x: (x.source != "topic", -x.works))
    return scored


def nearest_topics(client, phrases: list[str], limit: int = 4) -> list[Term]:
    """Closest curated OpenAlex topics to the phrases in a title.

    The volume filter answers "does this phrase retrieve literature". When
    nothing passes it, the useful question is different: "what is the nearest
    subject the index does know about". OpenAlex's own topic search answers
    that — it matches a coined or acronym-heavy phrase to a curated label
    with real papers behind it, so a title that yields no usable n-gram can
    still be pointed at a real field.

    These are suggestions, not validated terms, and are marked as such.
    """
    seen: dict[str, Term] = {}

    for phrase in phrases[:4]:
        try:
            page = client._get("/topics", search=phrase, per_page=3)
        except Exception:  # noqa: BLE001
            continue

        for topic in page.get("results", []):
            name = (topic.get("display_name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen[name.lower()] = Term(
                name.lower(),
                "suggested",
                works=int(topic.get("works_count") or 0),
                note=f"closest match to '{phrase}'",
            )

    out = sorted(seen.values(), key=lambda t: -t.works)
    return out[:limit]


def db_terms(client, seed: str, limit: int = 2) -> list[Term]:
    """Subject categories taken from the journals a seed term retrieves.

    Terms so far are phrases we hope some journal indexes. This closes the
    loop the other way: fetch the journals the best phrase actually returns,
    look up what SCImago files them under, and offer the dominant category
    as a term. It is guaranteed to have journals behind it, because it was
    read off those journals — and it is broader than the phrase, which is
    what makes it useful when the phrase alone shortlists three titles.

    Silent if SCImago is not loaded; this is an addition, never a dependency.
    """
    try:
        from .vocabulary import categories_of

        sources = client.journals_by_subject(seed, limit=25)
        issns = [s.get("issn_l") for s in sources if s.get("issn_l")]
        if not issns:
            return []

        return [
            Term(m.name, "category", works=m.journals,
                 note=f"{m.score:.0%} of journals found for '{seed}'")
            for m in categories_of(issns, limit=limit)
        ]
    except Exception:  # noqa: BLE001
        return []


def terms_from_title(client, title: str, max_terms: int = 5) -> list[Term]:
    """Full pipeline: title -> ranked, validated search terms.

    Candidates are ordered before measuring, not after, so the request budget
    goes to the phrases most likely to be real subjects: curated topics
    first, then known domain phrases, then trigrams, then bigrams.
    """
    candidates: list[Term] = []

    for name in topics_for_title(client, title):
        candidates.append(Term(name.lower(), "topic"))

    for phrase in candidate_phrases(title):
        if not any(phrase in c.text for c in candidates):
            source = "phrase" if phrase in KNOWN_PHRASES else "ngram"
            candidates.append(Term(phrase, source))

    # Longer n-grams are more likely to name a subject than a discipline, so
    # they go first and get measured before the budget runs out.
    order = {"topic": 0, "phrase": 1, "ngram": 2}
    candidates.sort(key=lambda t: (order.get(t.source, 3),
                                   -len(t.text.split())))

    scored = score_terms(client, candidates)

    # Drop near-duplicates ("digital twin" vs "digital twin building"),
    # keeping whichever came first — topics lead, then the largest counts.
    final: list[Term] = []
    for t in scored:
        if any(t.text in f.text or f.text in t.text for f in final):
            continue
        final.append(t)
        if len(final) >= max_terms:
            break

    if final:
        # One probe against the journal database, using the best phrase, to
        # add categories that provably have journals behind them.
        for t in db_terms(client, final[0].text):
            if not any(t.text in f.text or f.text in t.text for f in final):
                final.append(t)
        return final

    # Nothing cleared even the loosest bar. Rather than hand back an empty
    # list, ask the index what subjects the title is nearest to — a coined
    # name or an acronym string has no n-gram worth searching, but it is
    # still about something.
    phrases = [c.text for c in candidates if c.source != "topic"]
    return nearest_topics(client, phrases or [title.lower()])
