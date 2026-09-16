"""Semantic relevance: does this journal actually publish work like yours?

Why keyword matching failed
---------------------------
OpenAlex's `title_and_abstract.search` is bag-of-words. Searching "web service
quality of service" matches any paper containing those words in any
arrangement — including a sustainability paper about "service quality" in
public transport, or an accounting paper about "web-based service delivery".
Counting those matches told us a journal had three hits; it did not tell us
those hits were about anything like the submitted paper.

Two signals replace the count
-----------------------------

1. **TF-IDF cosine similarity.** Fetch the titles of each candidate journal's
   recent articles, build one document per journal, and compare against the
   user's title and abstract in a shared TF-IDF space. A journal whose
   vocabulary genuinely overlaps the paper scores high; one that shares two
   common words does not. This is the workhorse, and it needs no dependency
   beyond scikit-learn, which is already required.

2. **OpenAlex topic overlap.** OpenAlex assigns curated topics to every work
   and aggregates them per source. Comparing the paper's inferred topics to a
   journal's topic distribution catches cases where vocabulary differs but the
   subject matches — an important complement, because TF-IDF is blind to
   synonyms.

The two are combined because they fail differently: TF-IDF misses paraphrase,
topic overlap misses fine distinctions within a topic.

Optional embeddings
-------------------
If `sentence-transformers` is installed, `embedding_similarity` uses it
instead of TF-IDF for the vocabulary signal. It handles paraphrase better, but
costs ~2GB of dependencies and a model download, so it is strictly opt-in and
the tool works fully without it.
"""

from __future__ import annotations

import re

import numpy as np

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAVE_SKLEARN = True
except ImportError:  # pragma: no cover
    HAVE_SKLEARN = False


# Words that appear in academic titles regardless of subject and therefore
# carry no discriminating signal.
ACADEMIC_STOPWORDS = {
    "using", "based", "novel", "new", "approach", "method", "methods",
    "framework", "model", "models", "system", "systems", "analysis", "study",
    "review", "towards", "toward", "via", "case", "application",
    "applications", "performance", "evaluation", "assessment", "comparison",
    "design", "development", "implementation", "proposed", "improved",
    "enhanced", "efficient", "effective", "robust", "optimal", "optimized",
    "investigation", "research", "paper", "article", "journal", "international",
    "advances", "advanced", "recent", "modern", "comprehensive", "empirical",
    "experimental", "theoretical", "numerical", "results", "data", "use",
}


def _tokenize(text: str) -> str:
    text = re.sub(r"[^a-zA-Z\s-]", " ", str(text).lower())
    words = [
        w for w in text.split()
        if len(w) > 2 and w not in ACADEMIC_STOPWORDS
    ]
    return " ".join(words)


class SemanticMatcher:
    """Scores journals by how closely their output resembles the paper."""

    def __init__(self, query_text: str, min_titles: int = 5):
        self.query = _tokenize(query_text)
        self.min_titles = min_titles
        self.query_topics: dict[str, float] = {}

    # ------------------------------------------------- vocabulary signal

    def tfidf_scores(self, corpora: dict[str, list[str]]) -> dict[str, float]:
        """Cosine similarity between the paper and each journal's corpus.

        `corpora` maps a journal key to the titles of its recent articles.
        Journals with too few titles score 0 — an unknown match is not a good
        one, and inventing a score for thin evidence is how the old counter
        produced its worst results.
        """
        if not HAVE_SKLEARN or not self.query.strip():
            return {}

        keys, docs = [], []
        for key, titles in corpora.items():
            usable = [t for t in titles if t and t.strip()]
            if len(usable) < self.min_titles:
                continue
            keys.append(key)
            docs.append(_tokenize(" ".join(usable)))

        if not docs:
            return {}

        try:
            vec = TfidfVectorizer(
                ngram_range=(1, 2),      # bigrams catch "web service"
                min_df=1,
                max_features=40_000,
                sublinear_tf=True,       # damps journals that repeat a word
            )
            matrix = vec.fit_transform(docs + [self.query])
        except ValueError:
            return {}

        sims = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
        return {k: float(s) for k, s in zip(keys, sims)}

    # ----------------------------------------------------- topic signal

    def set_query_topics(self, topics: dict[str, float]) -> None:
        total = sum(topics.values()) or 1.0
        self.query_topics = {k.lower(): v / total for k, v in topics.items()}

    def topic_scores(self, journal_topics: dict[str, dict[str, float]]) -> dict[str, float]:
        """Weighted overlap between the paper's topics and the journal's.

        Catches subject matches that TF-IDF misses because the wording differs
        — 'runtime adaptation' and 'self-healing systems' share few words but
        sit in the same OpenAlex topic.
        """
        if not self.query_topics:
            return {}

        out = {}
        for key, topics in journal_topics.items():
            if not topics:
                out[key] = 0.0
                continue
            total = sum(topics.values()) or 1.0
            norm = {k.lower(): v / total for k, v in topics.items()}
            overlap = sum(
                min(w, norm.get(name, 0.0))
                for name, w in self.query_topics.items()
            )
            out[key] = float(overlap)
        return out

    # -------------------------------------------------------- combined

    def combine(
        self,
        tfidf: dict[str, float],
        topics: dict[str, float],
        keyword_share: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Blend the signals into one 0-1 relevance score.

        Weights reflect how much each can be trusted. TF-IDF is measured
        directly from what the journal published, so it leads. Topics are
        curated but coarse. The keyword share is the old signal, kept at a low
        weight as a tiebreak rather than dropped, since it is the only one
        that survives when a journal's titles cannot be fetched.
        """
        keys = set(tfidf) | set(topics) | set(keyword_share or {})
        if not keys:
            return {}

        def norm(d: dict[str, float]) -> dict[str, float]:
            if not d:
                return {}
            vals = [v for v in d.values() if v is not None]
            hi = max(vals) if vals else 0.0
            if hi <= 0:
                return {k: 0.0 for k in d}
            return {k: (v or 0.0) / hi for k, v in d.items()}

        nt, np_, nk = norm(tfidf), norm(topics), norm(keyword_share or {})

        out = {}
        for k in keys:
            t = nt.get(k)
            p = np_.get(k)
            w = nk.get(k, 0.0)

            if t is None and p is None:
                out[k] = 0.3 * w        # keyword evidence only — weak
                continue
            if t is None:
                out[k] = 0.65 * p + 0.35 * w
                continue
            if p is None:
                out[k] = 0.75 * t + 0.25 * w
                continue

            out[k] = 0.55 * t + 0.30 * p + 0.15 * w

        return out


def fetch_journal_corpora(
    client,
    source_ids: dict[str, str],
    per_journal: int = 60,
    progress: bool = True,
) -> tuple[dict[str, list[str]], dict[str, dict[str, float]]]:
    """Fetch recent article titles and topic distributions per journal.

    One API call each, run sequentially to respect the politeness delay. The
    same call returns both signals, so this does not double the cost.
    """
    from datetime import date

    y_from = date.today().year - 5
    corpora: dict[str, list[str]] = {}
    topics: dict[str, dict[str, float]] = {}

    total = len(source_ids)
    for i, (key, sid) in enumerate(source_ids.items(), 1):
        short = str(sid).rsplit("/", 1)[-1]
        try:
            page = client._get(
                "/works",
                filter=f"primary_location.source.id:{short},"
                       f"publication_year:>{y_from},type:article|review",
                per_page=per_journal,
                sort="cited_by_count:desc",   # representative, not incidental
                select="title,topics",
            )
        except Exception:  # noqa: BLE001
            continue

        titles, tcount = [], {}
        for w in page.get("results", []):
            if w.get("title"):
                titles.append(w["title"])
            for t in (w.get("topics") or [])[:2]:
                name = t.get("display_name")
                if name:
                    tcount[name] = tcount.get(name, 0) + 1

        corpora[key] = titles
        topics[key] = tcount

        if progress and (i % 20 == 0 or i == total):
            print(f"    profiled {i}/{total} journals", flush=True)

    return corpora, topics


def query_topics_from_openalex(client, text: str, sample: int = 30) -> dict[str, float]:
    """Infer the paper's topics by looking at what OpenAlex returns for it."""
    try:
        page = client._get(
            "/works", search=text[:350], per_page=sample, select="topics"
        )
    except Exception:  # noqa: BLE001
        return {}

    counts: dict[str, float] = {}
    for w in page.get("results", []):
        for t in (w.get("topics") or [])[:2]:
            name = t.get("display_name")
            if name:
                counts[name] = counts.get(name, 0) + 1.0
    return counts


def embedding_similarity(query: str, corpora: dict[str, list[str]]) -> dict[str, float]:
    """Optional: sentence-transformer similarity instead of TF-IDF.

    Handles paraphrase better, but requires ~2GB of dependencies and a model
    download. Returns {} when unavailable so callers fall back silently.
    """
    try:
        from sentence_transformers import SentenceTransformer, util
    except ImportError:
        return {}

    model = SentenceTransformer("all-MiniLM-L6-v2")
    keys = [k for k, v in corpora.items() if v]
    docs = [" ".join(corpora[k][:40]) for k in keys]
    if not docs:
        return {}

    qv = model.encode(query, convert_to_tensor=True)
    dv = model.encode(docs, convert_to_tensor=True)
    sims = util.cos_sim(qv, dv).cpu().numpy().ravel()
    return {k: float(s) for k, s in zip(keys, sims)}
