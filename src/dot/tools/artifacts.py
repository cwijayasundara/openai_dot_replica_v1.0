"""Files the tools point at by id. The model receives the id, not the body."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{16}")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put_text(self, body: str) -> str:
        artifact_id = "art_" + uuid.uuid4().hex[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / artifact_id).write_text(body, encoding="utf-8")
        return artifact_id

    def read_text(self, artifact_id: str) -> str:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise KeyError(artifact_id)
        path = self.root / artifact_id
        if not path.is_file():
            raise KeyError(artifact_id)
        return path.read_text(encoding="utf-8")
