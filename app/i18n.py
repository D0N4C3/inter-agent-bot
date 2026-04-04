from __future__ import annotations

import json
from pathlib import Path

from app.config import settings

_TRANSLATIONS_PATH = Path(__file__).resolve().parent / "data" / "translations.json"
_MINI_APP_STRINGS_EN_PATH = Path(__file__).resolve().parent / "data" / "mini_app_strings_en.json"
_MINI_APP_STRINGS_AM_PATH = Path(__file__).resolve().parent / "data" / "mini_app_strings_am.json"
_MINI_APP_STRINGS_OM_PATH = Path(__file__).resolve().parent / "data" / "mini_app_strings_om.json"
_MINI_APP_STRINGS_TI_PATH = Path(__file__).resolve().parent / "data" / "mini_app_strings_ti.json"


def load_translations() -> dict[str, dict[str, str]]:
    with _TRANSLATIONS_PATH.open(encoding="utf-8") as file:
        translations: dict[str, dict[str, str]] = json.load(file)
    translations.setdefault("en", {})["timeline"] = settings.expected_review_timeline
    return translations


def load_mini_app_strings() -> dict[str, dict[str, str]]:
    with _MINI_APP_STRINGS_EN_PATH.open(encoding="utf-8") as file:
        en = json.load(file)
    with _MINI_APP_STRINGS_AM_PATH.open(encoding="utf-8") as file:
        am = json.load(file)
    with _MINI_APP_STRINGS_OM_PATH.open(encoding="utf-8") as file:
        om = json.load(file)
    with _MINI_APP_STRINGS_TI_PATH.open(encoding="utf-8") as file:
        ti = json.load(file)
    return {"en": en, "am": am, "om": om, "ti": ti}
