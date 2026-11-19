"""Concurrent first use of a schema's lazily-built classes and caches.

View classes, Python enums and compiled converters are built on first use.
Several threads touching a fresh schema at once must never see a half-built
class, and must all get the same enum class (so `is` checks hold).
"""

import sys
import threading

import pytest

import flatview

SRC = ("enum C : ubyte { Red, Green } "
       "table A { " + " ".join(f"f{i}: int;" for i in range(60)) + " k: int (key); c: C; } "
       "table R { a: [A]; } root_type R;")


@pytest.fixture(autouse=True)
def frequent_switches():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)          # make thread switches inside builds likely
    yield
    sys.setswitchinterval(old)


def hammer(work, threads=8, trials=60):
    errors = []
    for _ in range(trials):
        s = flatview.parse_fbs(SRC)       # fresh schema: nothing built yet
        data = s.encode({"a": [{"k": i, "f59": i, "c": 1} for i in range(3)]})
        root = s.read(data)
        barrier = threading.Barrier(threads)
        results = []

        def run():
            barrier.wait()
            try:
                results.append(work(root))
            except Exception as e:        # noqa: BLE001 - any failure is the finding
                errors.append(f"{type(e).__name__}: {e}")
        ts = [threading.Thread(target=run) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        yield results
    assert not errors, sorted(set(errors))[:3]


def test_view_classes_never_seen_half_built():
    for results in hammer(lambda root: root.a.lookup(1).f59):
        assert results == [1] * 8


def test_one_python_enum_class_per_enum():
    for results in hammer(lambda root: type(root.a[0].c)):
        assert len(set(results)) == 1


def test_compiled_converters_concurrently():
    for results in hammer(lambda root: root._asdict()["a"][2]["f59"]):
        assert results == [2] * 8
