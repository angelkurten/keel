"""Identifier helpers shared by the templates."""

from __future__ import annotations

import re


def pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[._\-\s]+", name) if p)


def camel(name: str) -> str:
    p = pascal(name)
    return p[:1].lower() + p[1:]


def snake(name: str) -> str:
    s = re.sub(r"[.\-\s]+", "_", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.lower()


def identifier(name: str) -> str:
    """A safe identifier for a literal value: 'in-review' -> 'in_review', '3d' -> '_3d'."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return s if re.match(r"[A-Za-z_]", s) else "_" + s
