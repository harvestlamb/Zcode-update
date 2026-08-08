"""Document retrieval for the ZCode mirror AI assistant.

Default backend: BM25 keyword retrieval over chunks of the markdown content.
This is dependency-free (a compact Okapi-BM25 implementation) and works well
for the ~50 small documents in this mirror.

Optional backend: dense retrieval via an OpenAI-compatible /v1/embeddings
endpoint + a FAISS index. Enabled only when EMBEDDINGS_BASE_URL is set in the
environment; otherwise BM25 is used. The LLM answer is the same either way —
only the retrieval layer differs.

Documents live at /data/content/{docs,changelog}/*.md, each with a small
front-matter block (title, url, type) that we parse for source attribution.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("zcode-ai.rag")

CONTENT_DIR = "/data/content"


@dataclass
class Chunk:
    """A retrievable text chunk with source metadata."""
    chunk_id: str
    text: str
    title: str
    url: str
    doc_type: str  # "docs" | "changelog"
    source_path: str  # e.g. "content/docs/agents.md"

    def source_dict(self, max_snippet: int = 160) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "type": self.doc_type,
            "snippet": self.text[:max_snippet].replace("\n", " ").strip(),
        }


# ---------------------------------------------------------------------------
# Loading + chunking
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_KV_RE = re.compile(r'^(\w+):\s*"?(.*?)"?\s*$', re.MULTILINE)


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = dict(_KV_RE.findall(m.group(1)))
    return meta, m.group(2).strip()


# CJK characters get expanded into single-char tokens so BM25 sees them.
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _tokenize(text: str) -> List[str]:
    """Lightweight tokenizer: lowercase ASCII words + individual CJK chars."""
    text = text.lower()
    # Split CJK runs into single chars, keep ASCII word runs.
    out: List[str] = []
    for word in re.findall(r"[a-z0-9_/.-]+|[\u4e00-\u9fff\u3400-\u4dbf]", text):
        if _CJK_RE.fullmatch(word):
            out.extend(list(word))
        else:
            out.append(word)
    # drop pure-punctuation / very short noise
    return [t for t in out if len(t) > 0 and not set(t) <= set("_-.")]


def _chunk_text(text: str, max_chars: int = 700, overlap: int = 120) -> List[str]:
    """Split body into overlapping chunks, preferring paragraph boundaries."""
    paragraphs = re.split(r"\n{2,}", text)
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= max_chars:
            buf = (buf + "\n\n" + para).strip() if buf else para
        else:
            if buf:
                chunks.append(buf)
            # start new buf with a small overlap for continuity
            buf = (buf[-overlap:] + "\n\n" + para).strip() if overlap and buf else para
    if buf:
        chunks.append(buf)
    return chunks or [text[:max_chars]]


def load_chunks(content_dir: str = CONTENT_DIR) -> List[Chunk]:
    """Load and chunk every markdown document under content_dir."""
    chunks: List[Chunk] = []
    if not os.path.isdir(content_dir):
        log.warning("Content dir %s does not exist yet.", content_dir)
        return chunks
    for sub in ("docs", "changelog"):
        sub_dir = os.path.join(content_dir, sub)
        if not os.path.isdir(sub_dir):
            continue
        for fn in sorted(os.listdir(sub_dir)):
            # Skip hidden / AppleDouble / backup files — only real *.md content.
            if fn.startswith(".") or fn.startswith("._") or not fn.endswith(".md"):
                continue
            path = os.path.join(sub_dir, fn)
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            meta, body = _parse_frontmatter(raw)
            title = meta.get("title") or fn[:-3]
            url = meta.get("url") or ""
            doc_type = meta.get("type") or sub
            source_path = f"content/{sub}/{fn}"
            slug = fn[:-3]
            for i, piece in enumerate(_chunk_text(body)):
                chunks.append(Chunk(
                    chunk_id=f"{slug}#{i}",
                    text=piece,
                    title=title,
                    url=url,
                    doc_type=doc_type,
                    source_path=source_path,
                ))
    log.info("Loaded %d chunks from %d documents.", len(chunks),
             len({c.source_path for c in chunks}))
    return chunks


# ---------------------------------------------------------------------------
# BM25 index (self-contained, no external deps)
# ---------------------------------------------------------------------------


class BM25Index:
    """Okapi-BM25 over a fixed corpus. Rebuilt on startup / import."""

    def __init__(self, chunks: List[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.tokenized: List[List[str]] = [_tokenize(c.text) for c in chunks]
        self.doc_len: List[int] = [len(toks) for toks in self.tokenized]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0

        # term frequencies per doc + document frequencies
        self.tf: List[Dict[str, int]] = []
        df: Dict[str, int] = {}
        for toks in self.tokenized:
            freqs: Dict[str, int] = {}
            for t in toks:
                freqs[t] = freqs.get(t, 0) + 1
            self.tf.append(freqs)
            for term in freqs:
                df[term] = df.get(term, 0) + 1
        self.idf: Dict[str, float] = {}
        n = len(chunks)
        for term, freq in df.items():
            # +1 smoothing to avoid negative idf for very common terms
            self.idf[term] = math.log(1 + (n - freq + 0.5) / (freq + 0.5))

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]:
        if not self.chunks:
            return []
        q_terms = _tokenize(query)
        if not q_terms:
            return []
        scores: List[float] = [0.0] * len(self.chunks)
        for term in q_terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, freqs in enumerate(self.tf):
                f = freqs.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (self.doc_len[i] / self.avgdl if self.avgdl else 0))
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(self.chunks[i], s) for i, s in ranked[:top_k] if s > 0]


# ---------------------------------------------------------------------------
# Top-level retriever used by app.py
# ---------------------------------------------------------------------------


class Retriever:
    """Holds the active index. Call .rebuild() after an import."""

    def __init__(self, content_dir: str = CONTENT_DIR):
        self.content_dir = content_dir
        self.chunks: List[Chunk] = []
        self.bm25: Optional[BM25Index] = None
        self._dense = None  # Optional dense backend (see below)
        self.rebuild()

    def rebuild(self) -> None:
        self.chunks = load_chunks(self.content_dir)
        self.bm25 = BM25Index(self.chunks)
        # Lazy dense backend: only build if embeddings env is configured.
        self._dense = _maybe_build_dense(self.chunks)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]:
        if self._dense is not None:
            return self._dense.search(query, top_k)
        return self.bm25.search(query, top_k) if self.bm25 else []

    def stats(self) -> dict:
        return {
            "chunks": len(self.chunks),
            "documents": len({c.source_path for c in self.chunks}),
            "backend": "dense" if self._dense else "bm25",
            "content_dir": self.content_dir,
        }


# ---------------------------------------------------------------------------
# Optional dense backend (OpenAI-compatible embeddings + cosine)
# ---------------------------------------------------------------------------

def _maybe_build_dense(chunks: List[Chunk]):
    """Build a dense retriever if EMBEDDINGS_BASE_URL is set; else return None."""
    base = os.environ.get("EMBEDDINGS_BASE_URL") or os.environ.get("LLM_BASE_URL")
    model = os.environ.get("EMBEDDINGS_MODEL", "bge-large-zh")
    api_key = os.environ.get("EMBEDDINGS_API_KEY") or os.environ.get("LLM_API_KEY", "")
    if not base:
        return None
    try:
        return _DenseRetriever(chunks, base, model, api_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("Dense retriever unavailable (%s); falling back to BM25.", exc)
        return None


class _DenseRetriever:
    """Cosine retriever over OpenAI-compatible embeddings.

    Stores vectors in memory (the corpus is tiny). Rebuilds from scratch on
    every .rebuild(), which is fine for ~50 docs.
    """

    def __init__(self, chunks: List[Chunk], base_url: str, model: str, api_key: str):
        import urllib.request  # local import; only needed for dense mode
        self.base = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.chunks = chunks
        self.vectors: List[List[float]] = []
        self._embed_store: List[List[float]] = []
        log.info("Building dense index (%d chunks, model=%s) ...", len(chunks), model)
        texts = [c.text for c in chunks]
        self.vectors = self._embed(texts)
        log.info("Dense index ready.")

    def _embed(self, texts: List[str]) -> List[List[float]]:
        import urllib.request, json as _json
        req = urllib.request.Request(
            f"{self.base}/v1/embeddings",
            data=_json.dumps({"input": texts, "model": self.model}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in payload["data"]]

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Chunk, float]]:
        qv = self._embed([query])[0]
        return _topk_cosine(self.vectors, qv, self.chunks, top_k)


def _topk_cosine(vectors, qv, chunks, top_k):
    # cosine similarity (vectors assumed pre-normalised by most embedding models)
    results = []
    for vec, chunk in zip(vectors, chunks):
        dot = sum(a * b for a, b in zip(vec, qv))
        results.append((chunk, dot))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]
