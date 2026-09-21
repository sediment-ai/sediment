# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Edit retention scorers for comparing applied edit text with a later observation.

**Two shapes, one valid scorer for each.**
``EditObservation``'s ``(applied_text, observed_file_text)`` pairs come in two shapes:
``Write`` pairs are whole-file-vs-whole-file, but Claude Code's dominant tool
call, ``Edit``, produces a **snippet-vs-whole-file** pair — ``applied_text``
is the small edited region (``new_string``), and ``observed_file_text`` is the
entire file at session end. A *symmetric* similarity score (one that normalizes by the
combined size of both strings) is structurally wrong for that shape: a
snippet that survives 100% intact inside a large file still scores near zero,
because the file's unrelated bulk swamps the denominator. The original
recommendation (``four_gram_survival``, below) was benchmarked only on
whole-file-vs-whole-file synthetic cases and never caught this.

``four_gram_containment`` is the scorer this module actually recommends for
``EditObservation``'s scorer seam — a *directional* score answering "what
fraction of ``applied_text`` remained inside ``observed_file_text``," invariant
to how much unrelated content the observed file carries. ``four_gram_survival`` (Copilot's own
``compute4GramTextSimilarity``, symmetric multiset overlap) is kept only for
cross-harness fidelity reference against Copilot's real signal — do not use it
to score an ``Edit`` pair.

``edit_distance_survival`` (normalized Levenshtein, also symmetric, also
broken on the snippet-vs-file shape) was dropped rather than restated as a
containment variant: a containment-shaped edit-distance needs windowed or
banded alignment to stay performant, and this scorer runs server-side, at
derive time, over the org's *entire* history — a full DP over two 256 KiB
strings (the size cap) is ~7*10^10 cells, infeasible. ``four_gram_containment``
is linear in the combined length of both strings.
"""

from __future__ import annotations

from collections import Counter


_FOUR_GRAM_SIZE = 4


def four_gram_containment(applied_text: str, observed_file_text: str) -> float:
    """The fraction of ``applied_text`` character 4-grams (by multiplicity)
    found in ``observed_file_text`` — the recommended edit-observation scorer.

    For each 4-gram in ``applied_text``, count how many times it recurs in
    ``observed_file_text``, capped at the applied text's own count for that gram
    (so extra occurrences elsewhere never over-credit); sum across all applied
    text grams and divide by the applied text's total gram count. This is
    directional — normalizing only by applied text size — so a snippet
    that survives verbatim inside a much larger file scores close to 1.0,
    regardless of how much unrelated content surrounds it. Compare
    ``four_gram_survival`` below, which is symmetric and fails exactly this
    case.

    Edge cases: ``applied_text == ""`` returns 1.0 (a trivially retained empty
    ``Write`` — nothing to lose). ``observed_file_text == ""`` returns 0.0
    unless the applied text is also empty (the file was deleted before session end — a
    real zero-survival observation, not a gap). Strings shorter than four
    characters fall back to exact/substring containment: 1.0 if the applied
    text is empty or occurs verbatim in the observed file, else 0.0 — the 4-gram
    machinery needs at least 4 characters to produce a single gram.
    """
    if not applied_text:
        return 1.0
    if not observed_file_text:
        return 0.0

    n = _FOUR_GRAM_SIZE
    if len(applied_text) < n:
        return 1.0 if applied_text in observed_file_text else 0.0

    applied_grams = _character_ngrams(applied_text, n)
    total = sum(applied_grams.values())
    if len(observed_file_text) < n:
        return 0.0
    observed_grams = _character_ngrams(observed_file_text, n)
    matched = sum(
        min(count, observed_grams.get(gram, 0)) for gram, count in applied_grams.items()
    )
    return matched / total


def four_gram_survival(applied_text: str, observed_file_text: str) -> float:
    """Return Copilot-compatible raw character four-gram similarity.

    **Symmetric — do not use this to score an ``Edit`` pair** (snippet vs.
    whole file); see the module docstring and ``four_gram_containment``
    above. Kept only so Copilot's own signal can be reproduced exactly for
    cross-harness fidelity comparisons (the pinning test against Copilot's
    real ``'hello'``->``'Hello'`` case stays meaningful for that purpose).

    Copilot's helper builds counted substring 4-grams for both strings, sums
    the absolute count differences per gram, and returns:

    ``(total_grams - different_grams) / total_grams``

    where ``total_grams`` is the number of 4-grams in both strings. This is a
    symmetric multiset overlap score, not token-set Jaccard. Strings shorter
    than four characters match Copilot's edge case exactly: 1.0 when identical,
    otherwise 0.0.
    """
    n = _FOUR_GRAM_SIZE
    if len(applied_text) < n or len(observed_file_text) < n:
        return 1.0 if applied_text == observed_file_text else 0.0

    applied_grams = _character_ngrams(applied_text, n)
    observed_grams = _character_ngrams(observed_file_text, n)
    total_gram_count = sum(applied_grams.values()) + sum(observed_grams.values())
    different_gram_count = sum(
        abs(applied_grams[gram] - observed_grams[gram])
        for gram in applied_grams.keys() | observed_grams.keys()
    )
    equal_gram_count = total_gram_count - different_gram_count
    return equal_gram_count / total_gram_count


def _character_ngrams(text: str, size: int) -> Counter[str]:
    return Counter(text[i : i + size] for i in range(len(text) - size + 1))
