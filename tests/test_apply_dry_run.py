"""Apply-simulation safety: the dry_run flag is honored by every applier.

The QA loop and any simulation must NEVER submit a real application. Each ATS
applier carries a dry_run flag (checked before the Submit click in its apply()
method). This locks in that the flag is threaded through construction.
"""

import pytest

from agents.auto_applier.greenhouse import GreenhouseAutoApplier
from agents.auto_applier.ashby import AshbyAutoApplier
from agents.auto_applier.lever import LeverAutoApplier
from agents.auto_applier.workday import WorkdayAutoApplier
from agents.auto_applier.generic import GenericAutoApplier

APPLIERS = [GreenhouseAutoApplier, AshbyAutoApplier, LeverAutoApplier,
            WorkdayAutoApplier, GenericAutoApplier]


@pytest.mark.parametrize("cls", APPLIERS)
def test_applier_honors_dry_run_true(cls):
    assert cls(profile={}, dry_run=True).dry_run is True


@pytest.mark.parametrize("cls", APPLIERS)
def test_applier_defaults_to_not_dry_run(cls):
    # Default must be a real run only when explicitly enabled by guardrails;
    # construction default is False, and the runner passes the guardrail value.
    assert cls(profile={}).dry_run is False
