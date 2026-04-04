from __future__ import annotations

import json
from pathlib import Path

from app.config import settings

_TRANSLATIONS_PATH = Path(__file__).resolve().parent / "data" / "translations.json"


def load_translations() -> dict[str, dict[str, str]]:
    with _TRANSLATIONS_PATH.open(encoding="utf-8") as file:
        translations: dict[str, dict[str, str]] = json.load(file)
    translations.setdefault("en", {})["timeline"] = settings.expected_review_timeline
    return translations

