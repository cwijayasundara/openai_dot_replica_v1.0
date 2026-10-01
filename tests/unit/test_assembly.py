from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from dot.assembly import build_graph_runtime
from dot.config import Settings


def test_unset_database_uses_memory() -> None:
    runtime = build_graph_runtime(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(runtime.checkpointer, MemorySaver)
    assert isinstance(runtime.store, InMemoryStore)
    runtime.close()
