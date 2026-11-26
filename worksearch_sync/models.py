"""Plain data containers shared across the package."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Target(Enum):
    """Portal state a tracker row maps to. Each value is the radio button's label."""

    INTERESTED = "Interested"
    APPLIED = "Applied"
    INTERVIEW = "Going for an interview"
    SUCCESSFUL = "Successful"
    UNSUCCESSFUL = "Unsuccessful"
    SKIP = "Skip"  # never sent to the portal

    @property
    def label(self) -> str:
        return self.value

    @property
    def radio_id(self) -> str:
        return f"clickable-{self.name}"


@dataclass
class Application:
    """One row of the tracker, normalised."""

    number: str
    applied_on: date
    company: str
    role: str
    status_text: str
    target: Target
    key: str  # "role - company", lower-cased; how the portal titles an entry
    note: str = ""
    eligible: bool = True  # False for skipped rows and future-dated rows

    @property
    def skipped(self) -> bool:
        return self.target is Target.SKIP

    @property
    def needs_followup(self) -> bool:
        # The add form only records an application date for "Applied", so every other
        # final state is added as Applied first and then updated.
        return self.target not in (Target.APPLIED, Target.INTERESTED)

    @property
    def title(self) -> str:
        return f"{self.role} - {self.company}"


@dataclass
class PortalEntry:
    """One entry read from the portal's job list."""

    key: str
    title: str
    status: str
    age_days: int | None
    update_url: str


@dataclass
class Plan:
    anchor: int | None  # index of the newest tracker row already on the portal (-1: none)
    updates: list[tuple[Application, PortalEntry]] = field(default_factory=list)
    additions: list[Application] = field(default_factory=list)
    cutoff: int = -1  # index of the last tracker row that should be logged
