from __future__ import annotations


def is_image_url(url: str | None) -> bool:
    if not url:
        return False
    return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp"))
