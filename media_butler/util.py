"""Small shared helpers."""

from __future__ import annotations


def safe_component(name: str, *, limit: int = 120) -> str:
    """Turn arbitrary text into a single safe path component."""
    cleaned = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    cleaned = " ".join(cleaned.split()).strip().strip(".")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(". ")
    return cleaned or "unnamed"


def human_size(num_bytes: int) -> str:
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
