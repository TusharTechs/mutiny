"""Mining the repository's own tests for construction sequences."""
from pathlib import Path

from mutiny.scenarios import construction_examples


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_finds_a_test_that_builds_the_receiver(tmp_path):
    _write(tmp_path, "pkg/cache.py", "class Cache:\n    def put(self, k):\n        pass\n")
    _write(tmp_path, "tests/test_cache.py", """
from pkg.cache import Cache


def test_put_evicts():
    cache = Cache(maxsize=2)
    cache.put("a")
    cache.put("b")
    assert len(cache) == 2
""")
    examples = construction_examples(
        tmp_path, "Cache.put", module_source=(tmp_path / "pkg/cache.py").read_text())
    assert len(examples) == 1
    assert "Cache(maxsize=2)" in examples[0][1]


def test_rejects_an_example_the_model_cannot_follow(tmp_path):
    """cachetools' mixin says self.Cache(...) and binds it in another file.

    Shown as a template it is worse than nothing: every probe copies self.Cache,
    every probe fails, and the measured yield fell from 24 usable inputs to 8.
    """
    _write(tmp_path, "pkg/cache.py", "class Cache:\n    def put(self, k):\n        pass\n")
    _write(tmp_path, "tests/__init__.py", """
class CacheTestMixin:
    def test_put(self):
        cache = self.Cache(maxsize=2)
        cache.put("a")
        self.assertEqual(len(cache), 1)
""")
    assert construction_examples(tmp_path, "Cache.put") == []


def test_keeps_it_when_the_binding_is_visible(tmp_path):
    _write(tmp_path, "pkg/cache.py", "class Cache:\n    def put(self, k):\n        pass\n")
    _write(tmp_path, "tests/test_lru.py", """
from pkg.cache import Cache


class LRUTest:
    Cache = Cache

    def test_put(self):
        cache = self.Cache(maxsize=2)
        cache.put("a")
        self.assertEqual(len(cache), 1)
""")
    examples = construction_examples(tmp_path, "Cache.put")
    assert len(examples) == 1
    assert "Cache = Cache" in examples[0][1], "the binding must travel with the test"


def test_a_fixture_argument_hides_the_construction(tmp_path):
    _write(tmp_path, "pkg/cache.py", "class Cache:\n    def put(self, k):\n        pass\n")
    _write(tmp_path, "tests/test_fixture.py", """
def test_put(cache):
    cache.put("a")
    assert len(cache) == 1
""")
    assert construction_examples(tmp_path, "Cache.put") == []


def test_library_code_is_never_mistaken_for_a_test(tmp_path):
    _write(tmp_path, "pkg/cache.py", """
class Cache:
    def put(self, k):
        pass


def test_helper():
    return Cache()
""")
    assert construction_examples(tmp_path, "Cache.put") == []
