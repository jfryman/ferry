import multiprocessing as mp

from ferry.locks import Lock, SQLiteLocks, new_lock


def test_acquire_get_release(tmp_path):
    store = SQLiteLocks(tmp_path / "locks.db")
    assert store.get("prod") is None
    assert store.try_acquire(new_lock("prod", 7, "abc", "pip")) is None
    held = store.get("prod")
    assert (held.key, held.holder, held.sha, held.user) == ("prod", 7, "abc", "pip")
    assert store.release("prod", 8) is False  # not the holder
    assert store.release("prod", 7) is True
    assert store.get("prod") is None
    assert store.release("prod", 7) is False


def test_second_acquire_returns_the_holder(tmp_path):
    store = SQLiteLocks(tmp_path / "locks.db")
    store.try_acquire(new_lock("prod", 7, "abc", "pip"))
    conflict = store.try_acquire(new_lock("prod", 8, "def", "ana"))
    assert isinstance(conflict, Lock) and conflict.holder == 7
    assert store.try_acquire(new_lock("staging", 8, "def", "ana")) is None  # other inventory is free


def test_locks_survive_a_restart(tmp_path):
    path = tmp_path / "nested" / "locks.db"
    SQLiteLocks(path).try_acquire(new_lock("prod", 7, "abc", "pip"))
    assert SQLiteLocks(path).get("prod").holder == 7  # a new process sees the same file


def _race(path, holder, results):
    results.put((holder, SQLiteLocks(path).try_acquire(new_lock("prod", holder, "sha", f"u{holder}")) is None))


def test_exactly_one_process_wins_a_race(tmp_path):
    path = tmp_path / "locks.db"
    SQLiteLocks(path)
    ctx = mp.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=_race, args=(path, h, results)) for h in range(1, 9)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    outcomes = [results.get(timeout=5) for _ in procs]
    winners = [h for h, won in outcomes if won]
    assert len(winners) == 1
    assert SQLiteLocks(path).get("prod").holder == winners[0]
