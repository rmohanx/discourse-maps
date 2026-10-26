from pathlib import Path

from discourse_maps.bibtex import normalize_doi, parse_file
from discourse_maps.enrich import abstract_from_inverted_index, strip_jats, title_similarity
from discourse_maps.obsidian import note_title
from discourse_maps.zitter import parse_paper_list

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_bibtex_converts_latex_and_normalizes_doi():
    records = {r.key: r for r in parse_file(FIXTURES / "sample.bib")}
    assert set(records) == {"Dir1928", "Sch1930", "Riv2001"}
    assert records["Dir1928"].doi == "10.1098/rspa.1928.0023"
    assert records["Sch1930"].title.startswith("Über die kräftefreie")
    assert records["Sch1930"].first_author_surname == "Schrödinger"
    assert records["Riv2001"].authors == ["Rivas, M.", "Somebody, A."]
    assert records["Riv2001"].entry_type == "book"


def test_normalize_doi_variants():
    assert normalize_doi("doi:10.1/ABC") == "10.1/abc"
    assert normalize_doi("https://dx.doi.org/10.1/x") == "10.1/x"
    assert normalize_doi("") is None


def test_title_similarity_ignores_word_order_and_case():
    assert title_similarity("Metamaterial Toroidal", "Toroidal metamaterial") == 1.0
    assert title_similarity("What is spin?", "The Atom and the Molecule") < 0.5


def test_abstract_rebuilt_from_inverted_index():
    assert abstract_from_inverted_index({"electron": [1], "The": [0], "spins": [2]}) == "The electron spins"
    assert abstract_from_inverted_index(None) is None


def test_strip_jats():
    jats = "<jats:title>Abstract</jats:title><jats:p>Spin is <jats:italic>real</jats:italic>.</jats:p>"
    assert strip_jats(jats) == "Spin is real ."


def test_note_title_is_a_safe_filename():
    assert note_title('Spin: a "model" / view?') == "@Spin a model view"


def test_parse_paper_list_rows():
    page = """
    <tr><th>Author</th><th>Year</th><th>Title</th></tr>
    <tr>
      <td>Dirac</td>
      <td>1928</td>
      <td><a href=" https://drive.google.com/file/d/abc-123/view">The Quantum Theory of the electron</a></td>
    </tr>"""
    (paper,) = parse_paper_list(page)
    assert (paper.author, paper.year, paper.drive_id) == ("Dirac", 1928, "abc-123")
