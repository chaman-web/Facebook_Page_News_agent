"""Focused tests for material claim matching."""

from pipeline.claim_matcher import compare_reports


def test_matching_reports_agree():
    result = compare_reports(
        "100 people killed after city attack",
        "Officials confirmed 100 people were killed after the attack.",
        "City attack leaves 100 dead",
        "Authorities said 100 people died in the city attack.",
    )

    assert result.matched is True
    assert result.contradictions == []


def test_conflicting_casualty_counts_are_material():
    result = compare_reports(
        "100 people killed after city attack",
        "Officials said 100 people were killed.",
        "City attack leaves 120 dead",
        "Authorities said 120 people died.",
    )

    assert result.matched is False
    assert any("counts" in item for item in result.contradictions)


def test_opposite_outcomes_are_material():
    result = compare_reports(
        "Parliament approves emergency measure",
        "Lawmakers approved the emergency measure.",
        "Parliament rejects emergency measure",
        "Lawmakers rejected the emergency measure.",
    )

    assert result.matched is False
    assert "conflicting outcome language" in result.contradictions


def test_republished_wire_copy_is_not_independent():
    result = compare_reports(
        "Central bank changes interest rates",
        "The central bank changed interest rates after its scheduled meeting.",
        "Central bank changes interest rates",
        "By Jane Reporter, Reuters - The central bank changed rates after its meeting.",
        primary_domain="reuters.com",
        other_domain="finance.example.com",
    )
    assert result.syndicated_copy is True
