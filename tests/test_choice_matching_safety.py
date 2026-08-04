"""The option matcher must never commit a plausible-but-wrong answer.

Every ATS renders a short stored answer ("Bachelor of Science") against its own
verbose vocabulary ("Bachelor of Science in Economics"). The matcher walks
tiers: exact -> synonym -> decline -> yes/no -> substring -> token overlap.

The token-overlap tier used to accept ANY option sharing one word with the
target, which silently filled:

    "Bachelor of Science"   -> "Master of Science"       (a degree he does not hold)
    "California Polytechnic State University" -> "Adams State University"
    "San Francisco"         -> "San Diego"

On a job application those are misrepresentations, not typos. The tier is now
gated: an option must cover EVERY distinctive word of the target, and must beat
the runner-up outright. Anything less returns None so the field is flagged for
the user's review instead of being filled with a confident wrong answer.

REJECT/ACCEPT below is the shared contract. tests/test_extension_choice_matching.py
runs the identical table against the extension's JavaScript matcher, so the
online (Python) and offline (JS) paths can never disagree.
"""

import pytest

from agents.autofill_mapper import _match_choice

# (target, options) pairs that MUST NOT produce a match. Each one was a real
# wrong answer before the gate.
REJECT = [
    ("Bachelor of Science", ["Master of Science", "Doctor of Philosophy"]),
    ("Master of Science", ["Bachelor of Science", "Doctor of Philosophy"]),
    ("California Polytechnic State University", ["Adams State University", "Boston College"]),
    ("Bachelor's Degree", ["Associate Degree", "Doctorate Degree"]),
    ("San Francisco", ["San Diego", "Kansas City"]),
    ("Data Scientist", ["Data Engineer", "Research Scientist"]),
    # target is only ever a SUFFIX here — neither option is "Economics plus a
    # qualifier", they are different subjects that happen to contain the word
    ("Economics", ["Home Economics", "Agricultural Economics"]),
    ("Bachelor of Science", ["Bachelor of Science in Physics", "Bachelor of Science in Economics"]),
]

# (target, options, expected) that MUST keep working — the tiers above overlap
# carry the real matches, and the gate must not break them.
ACCEPT = [
    ("Full-time", ["Part-time employee", "Full-time employee"], "Full-time employee"),
    ("Two or More Races", ["Asian", "Two or More Races (Not Hispanic or Latino)"],
     "Two or More Races (Not Hispanic or Latino)"),
    ("Yes", ["Yes, I have a disability", "No, I do not"], "Yes, I have a disability"),
    ("No", ["Hispanic or Latino", "Not Hispanic or Latino"], "Not Hispanic or Latino"),
    ("Male", ["Man", "Woman"], "Man"),
    ("Decline to answer", ["Male", "I don't wish to answer"], "I don't wish to answer"),
    ("California Polytechnic State University",
     ["California Polytechnic State University - San Luis Obispo", "Adams State University"],
     "California Polytechnic State University - San Luis Obispo"),
    ("Asian", ["Asian (Not Hispanic or Latino)", "White"], "Asian (Not Hispanic or Latino)"),
    # exact wins even when a longer option also covers it
    ("Economics", ["Economics", "Economics and Finance"], "Economics"),
]


@pytest.mark.parametrize("target,options", REJECT, ids=[t for t, _ in REJECT])
def test_never_commits_a_wrong_answer(target, options):
    got = _match_choice(target, options)
    assert got is None, (
        f"matched {target!r} -> {got!r}; none of {options} is {target!r}. "
        f"Filling a wrong answer here misrepresents the applicant."
    )


@pytest.mark.parametrize("target,options,expected", ACCEPT,
                         ids=[t for t, _, _ in ACCEPT])
def test_still_matches_real_equivalents(target, options, expected):
    assert _match_choice(target, options) == expected


def test_empty_options_passes_the_value_through():
    """No vocabulary to match against (free-text field) — the value stands."""
    assert _match_choice("Anything", []) == "Anything"
