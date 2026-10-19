"""Match the paper's treatment labels (from the manuscript extraction) to the data file's treatment names.

The workbench first asks the LLM (manuscript_extract.map_treatments) and uses its links via resolve_with;
this deterministic matcher is the fallback when the assist is off, the call fails or a label is unmapped.

Lenient by design: data files abbreviate ('03-CT', 'NT_soybeans') and papers use related but different
wording ('Conventional tillage', 'CT'). A paper label matches a data name when, after lower-casing and
splitting on punctuation, any of these holds for the label, its listed name or its abbreviation:
  - the whole strings are equal;
  - every word of the label appears in the data name;
  - the label (or its initials, e.g. 'conventional tillage' -> 'ct') is one of the data name's words;
  - the strings are near-identical (rapidfuzz ratio >= 85).
A factor level such as 'CT' therefore matches every data treatment that carries it ('03-CT', '04-CT-Urea').
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_STOP = {'and', 'with', 'the', 'of', 'a', 'an', 'plus', 'treatment', 'plot', 'plots'}


def _norm(s) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()


def _words(s) -> set[str]:
    return set(_norm(s).split())


def _initials(s) -> str:
    words = [w for w in _norm(s).split() if w not in _STOP]
    return ''.join(w[0] for w in words) if len(words) > 1 else ''


def _aliases(label: str, paper_treatments: list[dict]) -> set[str]:
    """The label itself plus the name/abbreviation of any paper treatment it refers to."""
    out = {label}
    for t in paper_treatments or []:
        names = {n for n in (t.get('name'), t.get('abbreviation')) if n}
        if any(_norm(label) == _norm(n) for n in names):
            out |= names
    return {a for a in out if _norm(a)}


def _matches(alias: str, data_name: str) -> bool:
    a, d, dw = _norm(alias), _norm(data_name), _words(data_name)
    if a == d:
        return True
    aw = _words(alias)
    if aw and aw <= dw:
        return True
    if len(a.replace(' ', '')) >= 2 and a.replace(' ', '') in dw:
        return True
    ini = _initials(alias)
    if len(ini) >= 2 and ini in dw:
        return True
    return fuzz.ratio(a, d) >= 85


def labels_in(facts: dict) -> list[str]:
    """Every treatment label the extraction used (treatment-specific values and events), in first-seen order."""
    seen = list(dict.fromkeys(list((facts.get('treatment_metadata') or {}).keys())
                              + [e['treatment'] for e in facts.get('events') or [] if e.get('treatment')]))
    return seen


def resolve_with(mapping: dict | None, tag: str, data_names: list[str],
                 paper_treatments: list[dict] | None = None) -> list[str]:
    """The LLM mapping's answer for ``tag`` when it has one, else the deterministic ``resolve``."""
    if mapping and mapping.get(tag):
        return [n for n in data_names if n in set(mapping[tag])]
    return resolve(tag, data_names, paper_treatments)  # unmapped, or the LLM found no link


def resolve(tag: str, data_names: list[str], paper_treatments: list[dict] | None = None) -> list[str]:
    """Data treatment names a paper tag applies to. A tag may list several labels ('CT, NT')."""
    labels = [p.strip() for p in re.split(r'[,;/]|\band\b', tag or '') if p.strip()]
    hits = []
    for name in data_names:
        if any(_matches(a, name) for lb in labels for a in _aliases(lb, paper_treatments)):
            hits.append(name)
    return hits


if __name__ == '__main__':  # self-check
    names = ['03-CT', '03-NT', '04-CT-Urea', '04-CsT-AA', 'CRP_reference', 'CT_soybeans', 'NT_soybeans']
    assert resolve('CT', names) == ['03-CT', '04-CT-Urea', 'CT_soybeans']
    assert resolve('Conventional tillage', names) == ['03-CT', '04-CT-Urea', 'CT_soybeans']
    assert resolve('no-till', names, [{'name': 'no-till', 'abbreviation': 'NT'}]) == ['03-NT', 'NT_soybeans']
    assert resolve('Reference', names) == ['CRP_reference']
    assert resolve('CT, NT', ['CT', 'NT', 'Control']) == ['CT', 'NT']
    assert resolve('Urea', names) == ['04-CT-Urea']
    assert resolve('Fallow', names) == []
    print('ok')
