"""The integration's strings and their English translation agree."""

from __future__ import annotations

import json
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "renac_local"


def test_translations_match_strings() -> None:
    strings = json.loads((INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == strings
