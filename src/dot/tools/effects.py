"""Every tool has one effect. Policy and profiles speak in these."""

from __future__ import annotations

from enum import StrEnum


class Effect(StrEnum):
    read = "read"
    draft = "draft"
    write = "write"
    external = "external"
    financial = "financial"
    credential = "credential"
