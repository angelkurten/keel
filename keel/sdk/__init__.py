"""SDK generators: the cartridge is the interface definition, these templates are the compilers.

    keel sdk typescript examples/fleet -o src/keel-fleet.ts

Every template reads the same input, `Cartridge.schema()` (what `keel schema` prints), and writes
one file for the product's repo: payload types, `emit` constructors, subject helpers and
constants. The runtime (HTTP client, envelope) is a published package per language.
"""

from __future__ import annotations

from typing import Any

from keel.cartridge import Cartridge
from keel.sdk import go, php, python, typescript

TEMPLATES = {
    "typescript": typescript.render,
    "ts": typescript.render,
    "python": python.render,
    "py": python.render,
    "go": go.render,
    "php": php.render,
}


def generate(language: str, cartridge: Cartridge, **options: Any) -> str:
    """`options`: `package` (Go package name, PHP namespace)."""
    try:
        render = TEMPLATES[language.lower()]
    except KeyError as exc:
        raise SystemExit(f"no template for '{language}' (have: typescript, python, go, php)") from exc
    return render(cartridge.schema(), **options)
