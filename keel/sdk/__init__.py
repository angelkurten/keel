"""SDK generators: the cartridge is the interface definition, these templates are the compilers.

    keel sdk typescript cartridges/fleet -o src/keel-fleet.ts

Every template reads the same input, `Cartridge.schema()` (what `keel schema` prints), and writes
one file for the product's repo: payload types, `emit` constructors, subject helpers and
constants. The runtime (HTTP client, envelope) is a published package per language.
"""

from __future__ import annotations

from keel.cartridge import Cartridge
from keel.sdk import typescript

TEMPLATES = {"typescript": typescript.render, "ts": typescript.render}


def generate(language: str, cartridge: Cartridge) -> str:
    try:
        render = TEMPLATES[language.lower()]
    except KeyError as exc:
        raise SystemExit(f"no template for '{language}' yet (have: typescript)") from exc
    return render(cartridge.schema())
