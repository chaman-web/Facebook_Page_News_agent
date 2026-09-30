"""Load only explicitly approved Group registry records."""

from __future__ import annotations

import json
from pathlib import Path

from distribution.models import GroupRecord


def load_groups(path: Path) -> list[GroupRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [GroupRecord(**item) for item in payload.get("groups", [])]
