"""Best-effort metadata enrichment from a DOI.

Ports the DOI lookups the offline ETL enrichment uses (``ETL/enrichment/
resolve_cite.py`` and ``fill_meta.py``) into a small, failure-tolerant service the
upload workbench can call. Network work is only a *fallback*: the deterministic
parsers and the DOI→link derivation already cover the common case, and every
exception here is swallowed so an unavailable API never breaks an upload or a
curator action.

Citations come from Crossref — the DOI registration agency's structured metadata,
the same source and format the ETL wrote — with Semantic Scholar as a fallback.
Abstracts come from Semantic Scholar.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

# These APIs are free and need no key at this volume; a polite User-Agent with a
# contact address keeps us in the good-behaving pool. One lookup per submission
# stays well inside the unauthenticated rate limits.
_CONTACT = "ryanackett@gmail.com"
_USER_AGENT = {"User-Agent": f"fluxapp-enrich/1.0 (mailto:{_CONTACT})"}
_TIMEOUT = 5.0
_S2_ABSTRACT_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
_S2_CITATION_URL = (
    "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    "?fields=title,authors,year,venue,journal,externalIds"
)
_CROSSREF_URL = "https://api.crossref.org/works/{doi}?mailto=" + _CONTACT


def _get_json(url: str, timeout: float) -> dict | None:
    """GET and parse JSON, or ``None`` on any failure. Never raises."""
    try:
        request = urllib.request.Request(url, headers=_USER_AGENT)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001 — enrichment is best-effort, never fatal
        return None


def clean_abstract(raw: str | None) -> str | None:
    """Strip JATS/HTML markup from an abstract, or ``None`` if nothing is left."""
    if not raw:
        return None
    text = re.sub(r"<jats:title>.*?</jats:title>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^abstract[:\s]+", "", text, flags=re.I).strip()
    return text or None


def fetch_abstract(doi: str, timeout: float = _TIMEOUT) -> str | None:
    """The paper's abstract from Semantic Scholar, or ``None`` on any miss/error."""
    bare = (doi or "").strip()
    if not bare:
        return None
    payload = _get_json(_S2_ABSTRACT_URL.format(doi=urllib.parse.quote(bare)), timeout)
    if not payload:
        return None
    return clean_abstract(payload.get("abstract"))


def _format_authors(authors) -> str:
    """Author list in the app's house style: ``Family, F. M., & Family, F. M.``.

    Handles Crossref (``family``/``given``) and Semantic Scholar (``name``) shapes.
    """
    names: list[str] = []
    for author in authors or []:
        if not isinstance(author, dict):
            continue
        family = (author.get("family") or "").strip()
        given = (author.get("given") or "").strip()
        if family:
            names.append(f"{family}, {given[0]}." if given else family)
            continue
        full = (author.get("name") or "").strip()
        if not full:
            continue
        parts = full.split()
        if len(parts) >= 2:
            names.append(f"{parts[-1]}, " + " ".join(f"{p[0]}." for p in parts[:-1]))
        else:
            names.append(full)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + ", & " + names[-1]


def _join_citation(authors, year, title, journal, volume, pages, doi) -> str | None:
    """Compose ``Authors (Year). Title. Journal, volume, pages. https://doi.org/…``."""
    title = (title or "").strip()
    if not title:
        return None
    parts = [f"{authors} " if authors else "", f"({year or 'n.d.'}). ", f"{title}."]
    if journal:
        tail = journal + (f", {volume}" if volume else "") + (f", {pages}" if pages else "")
        parts.append(f" {tail}.")
    if doi:
        parts.append(f" https://doi.org/{doi}")
    return re.sub(r"\s+", " ", "".join(parts)).strip() or None


def _citation_from_crossref(message) -> str | None:
    if not isinstance(message, dict):
        return None
    date_parts = (message.get("issued") or {}).get("date-parts") or []
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    return _join_citation(
        authors=_format_authors(message.get("author")),
        year=str(year) if year else None,
        title=" ".join(message.get("title") or []),
        journal=" ".join(message.get("container-title") or []).strip(),
        volume=str(message.get("volume") or "").strip(),
        pages=str(message.get("page") or "").strip(),
        doi=str(message.get("DOI") or "").strip(),
    )


def _citation_from_s2(paper) -> str | None:
    if not isinstance(paper, dict):
        return None
    journal = paper.get("journal") or {}
    return _join_citation(
        authors=_format_authors(paper.get("authors")),
        year=str(paper.get("year") or "").strip() or None,
        title=paper.get("title") or "",
        journal=(journal.get("name") or paper.get("venue") or "").strip(),
        volume=str(journal.get("volume") or "").strip(),
        pages=str(journal.get("pages") or "").strip(),
        doi=(paper.get("externalIds") or {}).get("DOI") or "",
    )


def fetch_citation(doi: str, timeout: float = _TIMEOUT) -> tuple[str, str] | None:
    """A formatted citation for ``doi`` and its source, or ``None``.

    Crossref first (authoritative structured metadata, matching the ETL's format),
    then Semantic Scholar. Returns ``(citation, "crossref"|"semantic_scholar")``.
    """
    bare = (doi or "").strip()
    if not bare:
        return None
    payload = _get_json(_CROSSREF_URL.format(doi=urllib.parse.quote(bare)), timeout)
    if payload:
        citation = _citation_from_crossref(payload.get("message"))
        if citation:
            return citation, "crossref"
    paper = _get_json(_S2_CITATION_URL.format(doi=urllib.parse.quote(bare)), timeout)
    if paper:
        citation = _citation_from_s2(paper)
        if citation:
            return citation, "semantic_scholar"
    return None
