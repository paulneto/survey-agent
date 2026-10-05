"""Direct tests for the seeded randomizer — it backs the engine's determinism guarantee (a resuming
respondent sees the identical order; an export can reproduce it), so it gets its own coverage rather
than only being exercised indirectly through the runtime."""
import pytest

from survey_agent.engine.randomize import seeded_order

ITEMS = [{"v": c} for c in "abcde"]


def key(o):
    return o["v"]


def order(items, **kw):
    return [key(o) for o in seeded_order(items, key, **kw)]


def test_same_seed_is_deterministic():
    assert order(ITEMS, seed_material="sess1|q1", mode="shuffle") == \
           order(ITEMS, seed_material="sess1|q1", mode="shuffle")


def test_different_seeds_can_differ_but_keep_the_same_membership():
    a = order(ITEMS, seed_material="sess1", mode="shuffle")
    b = order(ITEMS, seed_material="sess2", mode="shuffle")
    assert set(a) == set(b) == set("abcde")
    # not a hard guarantee for any single pair, but across these two seeds the orders differ
    assert a != b


def test_none_mode_preserves_author_order():
    assert order(ITEMS, seed_material="x", mode="none") == list("abcde")
    assert order(ITEMS, seed_material="x", mode=None) == list("abcde")


def test_reverse_mode():
    assert order(ITEMS, seed_material="x", mode="reverse") == list("edcba")


def test_fixed_positions_are_anchored_to_their_original_index():
    # "a" first and "e" last are locked; the middle (b,c,d) shuffles around them
    out = order(ITEMS, seed_material="sess1|q1", mode="shuffle", fixed={"a", "e"})
    assert out[0] == "a" and out[-1] == "e"
    assert set(out) == set("abcde")


def test_reverse_respects_fixed_anchors():
    # only b,c,d reverse; a and e stay pinned at index 0 and 4
    out = order(ITEMS, seed_material="x", mode="reverse", fixed={"a", "e"})
    assert out == ["a", "d", "c", "b", "e"]


def test_edge_cases_empty_and_single():
    assert seeded_order([], key, seed_material="x", mode="shuffle") == []
    assert order([{"v": "only"}], seed_material="x", mode="shuffle") == ["only"]


def test_all_fixed_is_identity():
    assert order(ITEMS, seed_material="x", mode="shuffle", fixed=set("abcde")) == list("abcde")


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        seeded_order(ITEMS, key, seed_material="x", mode="spin")
