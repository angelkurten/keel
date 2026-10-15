"""Keel: a context layer for AI agents in vertical SaaS.

Domain events in, a bitemporal context graph per tenant out,
shared by every agent through five tools.
"""

from keel.cartridge import Cartridge, load_cartridge
from keel.envelope import Event
from keel.handlers import HandlerRegistry
from keel.ingest import Ingestor
from keel.scopes import Scope
from keel.store.memory import MemoryStore
from keel.store.mysql import MysqlStore
from keel.store.sqlite import SqliteStore
from keel.tools import Tools

__all__ = [
    "Cartridge",
    "Event",
    "HandlerRegistry",
    "Ingestor",
    "MemoryStore",
    "MysqlStore",
    "Scope",
    "SqliteStore",
    "Tools",
    "load_cartridge",
]

__version__ = "0.11.0"
