"""Static contract check: both backends implement exactly the declared surface.

Catches drift (a method added to one backend but not the other, or added to a
backend but never declared in the Protocol) without needing a database.
"""

import inspect

import pytest

from loom.storage.base import StorageBackend
from loom.storage.sqlite import LoomStorage

try:
    from loom.storage.postgres import PostgresStorage
except ImportError:
    PostgresStorage = None

CONTRACT = {
    name
    for name, member in inspect.getmembers(StorageBackend)
    if not name.startswith("_") and callable(member)
}

BACKENDS = [LoomStorage] + ([PostgresStorage] if PostgresStorage else [])


def _public_methods(cls) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls)
        if not name.startswith("_")
        and (inspect.isfunction(member) or inspect.ismethod(member))
    }


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda c: c.__name__)
def test_backend_implements_contract(backend):
    missing = CONTRACT - _public_methods(backend)
    assert not missing, f"{backend.__name__} missing contract methods: {sorted(missing)}"


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda c: c.__name__)
def test_backend_has_no_undeclared_public_methods(backend):
    # `conn` is a property/attribute, not part of the callable contract.
    extra = _public_methods(backend) - CONTRACT - {"conn"}
    assert not extra, (
        f"{backend.__name__} has public methods not in the StorageBackend "
        f"contract: {sorted(extra)} — declare them in storage/base.py so both "
        "backends must implement them"
    )


def test_contract_is_runtime_checkable(tmp_path):
    storage = LoomStorage(str(tmp_path / "c.db"))
    assert isinstance(storage, StorageBackend)
