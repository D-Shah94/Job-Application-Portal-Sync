"""Small JSON files kept in the data directory: the ledger and the remembered tracker path."""
import json
from pathlib import Path

from .config import CONFIG_FILE, DATA_DIR, LEDGER_FILE


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_ledger() -> list[dict]:
    data = _read(LEDGER_FILE, [])
    return data if isinstance(data, list) else []


def save_ledger(entries: list[dict]) -> None:
    _write(LEDGER_FILE, entries)


def load_config() -> dict:
    data = _read(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    _write(CONFIG_FILE, cfg)
