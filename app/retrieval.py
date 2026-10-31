"""Policy RAG: section chunking + BM25 ranking + relevance threshold + abstention.

One chunk per policy ID (e.g. VIA-FEE-004), so citations are real and stable.
Pure-Python BM25 (no vector DB) keeps the pipeline small and explainable.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import snowballstemmer

from . import config

_stem = snowballstemmer.stemmer("english").stemWord
_STOP = set("""a an the and or but if of to in on at for from by with about as is are was were be been am do does did
can could should would will shall may might i me my we our you your it its this that these those what which who how when
where why there here not no yes any some please tell give show want need get have has had than then so very just also
today now into over under up down out off again more most other such only own same too s t many long much until like whose whom tell""".split())
# Domain normalisation so "failed"/"failure", "paid"/"payment" meet at the same token.
_SYN = {"failure": "fail", "failed": "fail", "failing": "fail", "fails": "fail",
        "paid": "pay", "payment": "pay", "payments": "pay", "paying": "pay", "pays": "pay",
        "fees": "fee", "scholarships": "scholarship", "tickets": "ticket", "days": "day"}

_POST = {"respond": "respons", "repli": "respons"}  # stemmer leaves respond/response apart
_HEAD = re.compile(r"^### `(VIA-[A-Z]+-\d{3})` — (.+)$")
_SECTION = re.compile(r"^## (.+)$")


@dataclass
class Chunk:
    chunk_id: str
    title: str
    section: str
    text: str
    source: str


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for t in toks:
        if t in _STOP or len(t) < 2:
            continue
        t = _SYN.get(t, t)
        st = _stem(t)
        out.append(_POST.get(st, st))
    return out


def load_chunks(path: str | None = None) -> list[Chunk]:
    path = Path(path or config.HANDBOOK_PATH)
    chunks: list[Chunk] = []
    section, cur = "", None
    for line in path.read_text(encoding="utf-8").splitlines():
        if (m := _SECTION.match(line)):
            section = m.group(1)
            cur = None
            continue
        if (m := _HEAD.match(line)):
            cur = Chunk(m.group(1), m.group(2), section, "", path.name)
            chunks.append(cur)
            continue
        if cur is not None and line.strip():
            cur.text += (" " if cur.text else "") + line.strip()
    return chunks


class PolicyIndex:
    K1, B, TITLE_BOOST = 1.5, 0.75, 2
    REL_CUTOFF = 0.4  # drop passages scoring < 40% of the best hit (noise)

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.docs = [tokenize(c.title) * self.TITLE_BOOST + tokenize(c.text) for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        # A query term absent from the whole handbook is maximally informative: it signals out-of-scope.
        self.idf_unseen = math.log(1 + (n + 0.5) / 0.5)

    def _score(self, i: int, q_terms: list[str]) -> tuple[float, list[str]]:
        score, matched = 0.0, []
        dl = len(self.docs[i])
        for t in set(q_terms):
            f = self.tf[i].get(t, 0)
            if not f:
                continue
            matched.append(t)
            score += self.idf[t] * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * dl / self.avgdl))
        return score, matched

    def coverage(self, q_terms: list[str], matched: list[str]) -> float:
        """Share of the query's information (idf-weighted) that this passage covers, 0..1."""
        uq = set(q_terms)
        total = sum(self.idf.get(t, self.idf_unseen) for t in uq)
        got = sum(self.idf[t] for t in matched)
        return got / total if total else 0.0

    def search(self, query: str, k: int | None = None, min_score: float | None = None,
               min_coverage: float | None = None) -> dict:
        k = k or config.RAG_TOP_K
        min_score = config.RAG_MIN_SCORE if min_score is None else min_score
        min_coverage = config.RAG_MIN_COVERAGE if min_coverage is None else min_coverage
        q = tokenize(query)
        ranked = []
        for i, c in enumerate(self.chunks):
            s, m = self._score(i, q)
            if s > 0:
                ranked.append((s, m, self.coverage(q, m), c))
        ranked.sort(key=lambda x: -x[0])
        top = ranked[0][0] if ranked else 0.0
        kept = [(s, m, cov, c) for s, m, cov, c in ranked
                if s >= min_score and cov >= min_coverage and s >= self.REL_CUTOFF * top][:k]
        results = [{
            "chunk_id": c.chunk_id, "title": c.title, "section": c.section,
            "score": round(s, 3), "coverage": round(cov, 2), "matched_terms": m,
            "excerpt": c.text[:config.RAG_EXCERPT_CHARS if rank == 0 else getattr(config, "RAG_EXTRA_EXCERPT_CHARS", 450)], "source": c.source,
        } for rank, (s, m, cov, c) in enumerate(kept)]
        return {
            "retrieval_mode": "lexical-bm25",
            "query_terms": q, "top_k": k, "min_score": min_score, "min_coverage": min_coverage,
            "results": results,
            "abstain": not results,
            "reason": None if results else "No handbook passage met the relevance threshold.",
        }


_index: PolicyIndex | None = None


def get_index() -> PolicyIndex:
    global _index
    if _index is None:
        _index = PolicyIndex(load_chunks())
    return _index


def search_policy(query: str) -> dict:
    return get_index().search(query)


def get_chunk(chunk_id: str) -> dict | None:
    for c in get_index().chunks:
        if c.chunk_id == chunk_id:
            return {"chunk_id": c.chunk_id, "title": c.title, "section": c.section, "text": c.text}
    return None
