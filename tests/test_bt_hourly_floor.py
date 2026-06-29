"""BT auto-apply hourly floor.

Never auto-submit a Behavioral Technician role known to pay below $30/hr, or
whose pay is unconfirmed. Non-floored archetypes are unaffected.
"""

from agents.auto_applier.runner import _passes_hourly_floor

MAP = {"behavioral_technician": 30}


def test_bt_above_floor_passes():
    assert _passes_hourly_floor("behavioral_technician", 32, 36, MAP)


def test_bt_single_rate_at_floor_passes():
    assert _passes_hourly_floor("behavioral_technician", 30, 30, MAP)


def test_bt_below_floor_excluded():
    # The $24/hr BIA-style role Matthew wants to beat
    assert not _passes_hourly_floor("behavioral_technician", 22, 24, MAP)


def test_bt_unconfirmed_pay_excluded():
    assert not _passes_hourly_floor("behavioral_technician", None, None, MAP)


def test_bt_range_top_meets_floor_passes():
    # $28-$35/hr -> top 35 >= 30 -> eligible
    assert _passes_hourly_floor("behavioral_technician", 28, 35, MAP)


def test_non_bt_archetypes_have_no_floor():
    assert _passes_hourly_floor("data_analyst", None, None, MAP)
    assert _passes_hourly_floor("ai_engineer", None, None, MAP)
