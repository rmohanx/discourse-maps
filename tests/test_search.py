import numpy as np

from discourse_maps.search import mmr


def _unit(*v):
    a = np.array(v, dtype=float)
    return a / np.linalg.norm(a)


QUERY = _unit(1, 0, 0)
# Two near-duplicates close to the query, and one less relevant but different item.
CANDIDATES = np.array([_unit(1, 0.1, 0), _unit(1, 0.12, 0), _unit(0.6, 0, 0.8)])


def test_serendipity_zero_is_plain_ranking():
    assert mmr(QUERY, CANDIDATES, k=3, serendipity=0.0) == [0, 1, 2]


def test_serendipity_prefers_variety_over_near_duplicates():
    picked = mmr(QUERY, CANDIDATES, k=2, serendipity=0.7)
    assert picked[0] == 0
    assert picked[1] == 2  # skips the near-duplicate


def test_mmr_never_returns_more_than_available():
    assert len(mmr(QUERY, CANDIDATES, k=10, serendipity=0.5)) == 3
