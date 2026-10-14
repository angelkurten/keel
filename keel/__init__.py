"""Keel: a context layer for AI agents in vertical SaaS.

Domain events in, a bitemporal context graph per tenant out,
shared by every agent through five tools.
"""

from keel.envelope import Event
from keel.handlers import HandlerRegistry
from keel.store.memory import MemoryStore
from keel.scopes import Scope
from keel.cartridge import Cartridge, load_cartridge
from keel.ingest import Ingestor
from keel.tools import Tools

__all__ = [
    "Event",
    "HandlerRegistry",
    "MemoryStore",
    "Scope",
    "Cartridge",
    "load_cartridge",
    "Ingestor",
    "Tools",
]

__version__ = "0.0.1"
