from datetime import date
from pathlib import Path

import pytest

from worksearch_sync.models import Target
from worksearch_sync.tracker import (TrackerError, classify_status, clean, load_records, make_key,
                                     parse_date, read_tracker, resolve_columns)

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_tracker.md"
TODAY = date(2026, 10, 1)


@pytest.mark.parametrize("text, expected", [
    ("2026-10-26", date(2026, 10, 26)),
    ("2026/10/26", date(2026, 10, 26)),
    ("26/10/2026", date(2026, 10, 26)),   # day first, not month first
    ("26-10-2026", date(2026, 10, 26)),
    ("26 Oct 2026", date(2026, 10, 26)),
    ("8 October 2026", date(2026, 10, 8)),
    ("2026-02-30", None),                 # not a real date
    ("", None),
    ("soon", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_clean_strips_markdown():
    assert clean("**[Acme](https://example.com)** `Ltd`") == "Acme Ltd"
    assert clean("  too   many\u00a0spaces ") == "too many spaces"


def test_key_matches_portal_title_format():
    assert make_key("Software Engineer", "Zone & Co") == "software engineer - zone & co"


@pytest.mark.parametrize("raw, expected", [
    ("Applied", Target.APPLIED),
    ("", Target.APPLIED),                       # no status means applied
    ("Phone screen", Target.INTERVIEW),
    ("Offer", Target.SUCCESSFUL),
    ("Rejected", Target.UNSUCCESSFUL),
    ("Evaluated", Target.SKIP),
    ("something unrecognised", Target.SKIP),    # unknown statuses are never sent
    ("Unsuccessful - no feedback", Target.UNSUCCESSFUL),  # "success" must not win over "unsuccessful"
])
def test_classify_status(raw, expected):
    assert classify_status(raw) is expected


def test_interested_needs_opt_in():
    assert classify_status("Saved") is Target.SKIP
    assert classify_status("Saved", include_interested=True) is Target.INTERESTED


def test_column_aliases():
    cols = resolve_columns(["No.", "Date Applied", "Employer", "Job Title", "Stage"])
    assert cols == {"number": "No.", "date": "Date Applied", "company": "Employer",
                    "role": "Job Title", "status": "Stage"}


def test_missing_required_column():
    with pytest.raises(TrackerError):
        resolve_columns(["Date", "Company"])


def test_read_sample_tracker():
    apps = read_tracker(SAMPLE, today=TODAY)
    assert [a.number for a in apps] == ["001", "002", "003", "004", "005", "006"]
    assert [a.target for a in apps] == [Target.APPLIED, Target.APPLIED, Target.SKIP,
                                        Target.INTERVIEW, Target.UNSUCCESSFUL, Target.APPLIED]
    assert apps[2].eligible is False  # skipped row


def test_future_dates_are_not_eligible(tmp_path, capsys):
    f = tmp_path / "t.csv"
    f.write_text("Date,Company,Role\n2030-01-01,Acme,Dev\n", encoding="utf-8")
    apps = read_tracker(f, today=TODAY)
    assert len(apps) == 1 and apps[0].eligible is False
    assert "future date" in capsys.readouterr().out


def test_csv_and_json_without_number_column(tmp_path):
    csv_file = tmp_path / "t.csv"
    csv_file.write_text("Date,Company,Role,Status\n2026-09-01,Acme,Dev,Applied\n", encoding="utf-8")
    json_file = tmp_path / "t.json"
    json_file.write_text('{"applications": [{"Date": "2026-09-01", "Company": "Acme", "Role": "Dev"}]}',
                         encoding="utf-8")
    assert read_tracker(csv_file, today=TODAY)[0].number == "001"
    assert read_tracker(json_file, today=TODAY)[0].number == "001"


def test_notes_mode(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("Date,Company,Role,Via,Notes\n2026-09-01,Acme,Dev,Indeed,Sent CV\n", encoding="utf-8")
    assert read_tracker(f, today=TODAY)[0].note == ""
    assert read_tracker(f, notes_mode="via", today=TODAY)[0].note == "Applied via Indeed"
    assert read_tracker(f, notes_mode="notes", today=TODAY)[0].note == "Sent CV"


def test_markdown_without_table(tmp_path):
    f = tmp_path / "t.md"
    f.write_text("just text", encoding="utf-8")
    with pytest.raises(TrackerError):
        load_records(f)
