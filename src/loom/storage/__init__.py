from .base import StorageBackend
from .sqlite import LoomStorage

try:
    from .postgres import PostgresStorage
except ImportError:
    PostgresStorage = None  # type: ignore[assignment,misc]


def _check_contract(storage):
    if not isinstance(storage, StorageBackend):
        missing = [
            name
            for name in dir(StorageBackend)
            if not name.startswith("_") and not hasattr(storage, name)
        ]
        raise TypeError(
            f"{type(storage).__name__} does not satisfy the storage contract; "
            f"missing: {', '.join(missing)}"
        )
    return storage


def create_storage(config=None):
    """Factory that returns the appropriate storage backend based on config."""
    if config is None:
        return _check_contract(LoomStorage())

    storage_cfg = config.storage if hasattr(config, "storage") else config
    backend = getattr(storage_cfg, "backend", "sqlite")
    if backend == "postgres":
        dsn = getattr(storage_cfg, "postgres_dsn", "")
        if not dsn:
            raise ValueError("storage.postgres_dsn is required when backend=postgres")
        if PostgresStorage is None:
            raise ImportError(
                "psycopg is required for the Postgres backend. "
                "Install with: pip install 'loom-gateway[postgres]'"
            )
        return _check_contract(PostgresStorage(dsn=dsn))
    return _check_contract(
        LoomStorage(db_path=getattr(storage_cfg, "database_path", "loom.db"))
    )


__all__ = ["LoomStorage", "PostgresStorage", "StorageBackend", "create_storage"]
