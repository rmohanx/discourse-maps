"""Parse BibTeX into plain records (LaTeX accents converted to Unicode)."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode


@dataclass
class BibRecord:
    key: str
    entry_type: str
    title: str
    authors: list[str]
    year: int | None
    doi: str | None
    url: str | None
    source_file: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def first_author_surname(self) -> str:
        if not self.authors:
            return ""
        name = self.authors[0]
        return (name.split(",")[0] if "," in name else name.split()[-1]).strip()


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", raw.strip(), flags=re.I)
    return doi.lower() or None


def _strip_braces(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "")).strip()


def parse_file(path: Path) -> list[BibRecord]:
    parser = BibTexParser(common_strings=True)
    parser.customization = convert_to_unicode
    parser.ignore_nonstandard_types = False
    db = bibtexparser.loads(path.read_text(encoding="utf-8"), parser=parser)
    records = []
    for entry in db.entries:
        year = re.search(r"\d{4}", entry.get("year", ""))
        records.append(
            BibRecord(
                key=entry["ID"],
                entry_type=entry.get("ENTRYTYPE", "misc"),
                title=_strip_braces(entry.get("title", "")),
                authors=[_strip_braces(a) for a in re.split(r"\s+and\s+", entry.get("author", "")) if a],
                year=int(year.group()) if year else None,
                doi=normalize_doi(entry.get("doi")),
                url=entry.get("url") or None,
                source_file=path.name,
                fields={k: v for k, v in entry.items() if k not in ("ID", "ENTRYTYPE")},
            )
        )
    return records
