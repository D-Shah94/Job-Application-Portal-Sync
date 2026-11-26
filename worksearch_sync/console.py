"""Keyboard handling and prompts."""
import os
import sys

if os.name == "nt":
    import msvcrt
else:
    import select

# Answers from the step prompt
CONTINUE, AUTO, MENU = "continue", "auto", "menu"


class SkipItem(Exception):
    """User chose to skip the current item at the step prompt."""


class CancelForm(Exception):
    """User abandoned the current form and wants the menu."""


def enter_pressed() -> bool:
    """True if ENTER was hit since the last call. Never blocks.

    A plain input() would freeze Playwright's event loop while we wait for the user to log in.
    """
    if os.name == "nt":
        pressed = False
        while msvcrt.kbhit():
            if msvcrt.getwch() in ("\r", "\n"):
                pressed = True
        return pressed
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.readline()
        return True
    return False


def ask_step(description: str) -> str:
    """Prompt shown before Save in step mode. Raises SkipItem/CancelForm for k/q."""
    while True:
        print(f"    [STEP] {description}")
        print("           [ENTER] save and continue   [y] save and run the rest automatically   [m] save, then menu")
        print("           [k] skip this item          [q] discard this form, back to menu")
        answer = input("           Action [ENTER/y/m/k/q]: ").strip().lower()
        if answer == "":
            return CONTINUE
        if answer == "y":
            return AUTO
        if answer == "m":
            return MENU
        if answer == "k":
            raise SkipItem()
        if answer == "q":
            raise CancelForm()
        print(f"           Please press ENTER or type y, m, k or q (got {answer!r}).")


MENU_TEXT = """
Menu ({updates} update(s), {additions} addition(s) remaining)
  [y] run everything remaining, no pauses
  [t] test one item (pauses before Save, then returns here)
  [s] step through items (pauses before each Save; 'y' at a pause switches to automatic)
  [u] run the remaining status updates only
  [a] run the remaining additions only
  [p] print the queue
  [r] re-scan the portal and rebuild the queue
  [f] choose a different tracker file
  [q] quit (anything already saved stays saved)"""


def ask_menu(updates: int, additions: int) -> str:
    while True:
        print(MENU_TEXT.format(updates=updates, additions=additions))
        answer = input("Choice [y/t/s/u/a/p/r/f/q]: ").strip().lower()
        if answer in ("y", "t", "s", "u", "a", "p", "r", "f", "q"):
            return answer
        if answer == "n":
            return "q"
        print(f"  Please choose one of y, t, s, u, a, p, r, f or q (got {answer!r}).")
