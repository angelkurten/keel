from keel.store.base import Entity, Fact, Relation, Store
from keel.store.memory import MemoryStore

__all__ = ["Entity", "Fact", "MemoryStore", "Relation", "Store", "open_store"]


def open_store(url: str | None) -> Store:
    """A store from a `--db` value: nothing for memory, mysql:// or mariadb:// for MySQL, else a SQLite path."""
    if not url:
        return MemoryStore()
    if url.startswith(("mysql://", "mariadb://")):
        from keel.store.mysql import MysqlStore

        return MysqlStore.from_url(url)
    from keel.store.sqlite import SqliteStore

    return SqliteStore(url)
