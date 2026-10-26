"""Fetch the Zitter Repository: BibTeX metadata, the paper list, and (optionally) PDFs.

Everything lands under data/ (git-ignored). PDFs stay local because they are publishers'
copyrighted works; the repository only ever stores metadata and links.
"""

import html
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from discourse_maps.config import DATA_DIR

PAGE_URL = "https://www.zitter-institute.org/p/zitter-repository.html"
RAW_DIR = DATA_DIR / "raw" / "zitter"
PDF_DIR = DATA_DIR / "pdfs" / "zitter"

# BibTeX files linked from the repository page, by Google Drive file id.
BIB_FILES = {
    "zitter-xx.bib": "1HgAOFW1M7szrmPkqWc8rhxl5IJcjY9D3",
    "zitter-xxi.bib": "17AAiYv4AJeXrPY9Xr7lwiAnJe_endF1Q",
    "zitter-extra.bib": "1UA068URQLaFF7ld3gGi1j-tCBvzL4lWH",
}

_ROW = re.compile(
    r"<tr>\s*<td>(?P<author>[^<]*)</td>\s*<td>\s*(?P<year>\d{4})\s*</td>\s*<td>\s*"
    r'<a [^>]*href="\s*https://drive\.google\.com/file/d/(?P<drive_id>[\w-]+)[^"]*"[^>]*>(?P<title>.*?)</a>',
    re.S,
)


@dataclass(frozen=True)
class Paper:
    author: str
    year: int
    title: str
    drive_id: str


def drive_download_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_paper_list(page_html: str) -> list[Paper]:
    return [
        Paper(_clean(m["author"]), int(m["year"]), _clean(m["title"]), m["drive_id"])
        for m in _ROW.finditer(page_html)
    ]


def fetch(client: httpx.Client | None = None) -> dict[str, int]:
    """Download the BibTeX files and the paper list into data/raw/zitter/."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = client or httpx.Client(follow_redirects=True, timeout=30)
    counts: dict[str, int] = {}
    for name, file_id in BIB_FILES.items():
        text = client.get(drive_download_url(file_id)).raise_for_status().text
        if "@" not in text[:2000]:
            raise RuntimeError(f"{name}: Drive did not return BibTeX (got {text[:80]!r})")
        (RAW_DIR / name).write_text(text, encoding="utf-8")
        counts[name] = text.count("\n@") + text.startswith("@")
    papers = parse_paper_list(client.get(PAGE_URL).raise_for_status().text)
    (RAW_DIR / "papers.json").write_text(
        json.dumps([asdict(p) for p in papers], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    counts["papers.json"] = len(papers)
    return counts


def load_papers() -> list[Paper]:
    path = RAW_DIR / "papers.json"
    return [Paper(**p) for p in json.loads(path.read_text(encoding="utf-8"))]


def fetch_pdfs(limit: int | None = None, delay: float = 2.0) -> dict[str, int]:
    """Download PDFs slowly (Drive rate-limits bursts). Skips files already present."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for paper in load_papers()[:limit]:
            target: Path = PDF_DIR / f"{paper.drive_id}.pdf"
            if target.exists():
                counts["skipped"] += 1
                continue
            resp = client.get(drive_download_url(paper.drive_id))
            if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
                target.write_bytes(resp.content)
                counts["downloaded"] += 1
            else:
                # Large files get a "can't scan for viruses" HTML page instead of the PDF.
                counts["failed"] += 1
            time.sleep(delay)
    return counts
