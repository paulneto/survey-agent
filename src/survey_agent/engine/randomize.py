"""Deterministic, seed-stable randomization with locked ("fixed") positions.

The seed is derived from the session id + a scope id, so a respondent who resumes sees
the identical order, and the realized order is reproducible offline for exports. Items
listed in `fixed` keep their original index (e.g. "Other" / "None of the above" anchored
at the end) while the rest are shuffled around them.
"""
import hashlib
import random


def _seed_int(material):
    return int(hashlib.sha256(str(material).encode()).hexdigest()[:16], 16)


def seeded_order(items, key_fn, seed_material, mode="shuffle", fixed=None):
    """Return `items` reordered deterministically.

    key_fn(item) -> the stable key used to match `fixed`.
    mode: "shuffle" | "rotate" | "reverse" | "none"/None
    fixed: iterable of keys whose ORIGINAL position is locked.
    """
    if mode in (None, "none"):
        return list(items)
    fixed = set(fixed or [])
    indexed = list(enumerate(items))
    locked = [(i, it) for i, it in indexed if key_fn(it) in fixed]
    movable = [it for _, it in indexed if key_fn(it) not in fixed]

    if mode == "shuffle":
        rng = random.Random(_seed_int(seed_material))
        rng.shuffle(movable)
    elif mode == "reverse":
        movable = list(reversed(movable))
    elif mode == "rotate":
        rng = random.Random(_seed_int(seed_material))
        if movable:
            k = rng.randrange(len(movable))
            movable = movable[k:] + movable[:k]
    else:
        raise ValueError(f"unknown randomization mode: {mode!r}")

    result = [None] * len(items)
    for i, it in locked:
        result[i] = it
    slot = 0
    for it in movable:
        while result[slot] is not None:
            slot += 1
        result[slot] = it
    return result
