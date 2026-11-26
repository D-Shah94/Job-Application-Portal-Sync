from datetime import date
from pathlib import Path

import pytest

from worksearch_sync.models import PortalEntry
from worksearch_sync.planner import PlanError, build_plan, ledger_indexes, match_entries
from worksearch_sync.tracker import make_key, read_tracker

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_tracker.md"
TODAY = date(2026, 10, 1)


@pytest.fixture
def apps():
    return read_tracker(SAMPLE, today=TODAY)


def entry(role, company, status="Applied", age=None):
    key = make_key(role, company)
    return PortalEntry(key, f"{role} - {company}", status, age, "https://portal/update/" + key)


def age_of(a):
    return (TODAY - a.applied_on).days


def test_unique_rows_match_on_text(apps):
    matched = match_entries(apps, [entry("Software Engineer", "Contoso Systems")], TODAY)
    assert list(matched) == [1]


def test_duplicate_rows_are_told_apart_by_date(apps):
    # rows 0 and 5 are the same job applied for twice, 14 days apart
    old = match_entries(apps, [entry("Junior Python Developer", "Northwind Ltd", age=age_of(apps[0]))], TODAY)
    new = match_entries(apps, [entry("Junior Python Developer", "Northwind Ltd", age=age_of(apps[5]))], TODAY)
    assert list(old) == [0]
    assert list(new) == [5]


def test_entries_not_in_tracker_are_ignored(apps):
    matched = match_entries(apps, [entry("Data Engineer (project X)", "Recruiter Co")], TODAY)
    assert matched == {}


def test_plan_continues_after_newest_match(apps):
    on_portal = [entry("Junior Python Developer", "Northwind Ltd", age=age_of(apps[0])),
                 entry("Software Engineer", "Contoso Systems")]
    plan = build_plan(apps, match_entries(apps, on_portal, TODAY), set())
    assert plan.anchor == 1
    assert [a.number for a in plan.additions] == ["004", "005", "006"]  # 003 is skipped


def test_plan_stops_at_last_loggable_row(apps):
    apps[-1].target = apps[2].target  # make the final row a skipped one
    apps[-1].eligible = False
    plan = build_plan(apps, {}, {1})
    assert [a.number for a in plan.additions] == ["004", "005"]
    assert plan.cutoff == 4


def test_status_change_becomes_an_update(apps):
    on_portal = [entry("Data Engineer", "Tailspin Toys", status="Applied")]  # tracker says Rejected
    plan = build_plan(apps, match_entries(apps, on_portal, TODAY), set())
    assert [(a.number, e.status) for a, e in plan.updates] == [("005", "Applied")]


def test_matching_status_needs_no_update(apps):
    on_portal = [entry("Data Engineer", "Tailspin Toys", status="Unsuccessful")]
    plan = build_plan(apps, match_entries(apps, on_portal, TODAY), set())
    assert plan.updates == []


def test_ledger_is_used_when_the_portal_shows_nothing(apps):
    ledger = [{"date": apps[3].applied_on.isoformat(), "key": apps[3].key}]
    idx = ledger_indexes(apps, ledger)
    assert idx == {3}
    plan = build_plan(apps, {}, idx)
    assert plan.anchor == 3
    assert [a.number for a in plan.additions] == ["005", "006"]


def test_first_run_starts_from_the_top(apps):
    plan = build_plan(apps, {}, set())
    assert plan.anchor == -1
    assert [a.number for a in plan.additions] == ["001", "002", "004", "005", "006"]


def test_from_number_overrides_the_anchor(apps):
    on_portal = [entry("Software Engineer", "Contoso Systems")]
    plan = build_plan(apps, match_entries(apps, on_portal, TODAY), set(), from_number="005")
    assert [a.number for a in plan.additions] == ["005", "006"]


def test_unknown_from_number(apps):
    with pytest.raises(PlanError):
        build_plan(apps, {}, set(), from_number="999")
