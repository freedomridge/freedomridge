"""Nobody may take a SECOND pool connection while already holding one.

`Database.fetch/fetchrow/fetchval/execute/executemany` (backend/database.py) each do
`async with self.pool.acquire()`. Calling one of them from inside an
`async with db.pool.acquire() as conn:` block therefore borrows a second connection from the same
pool while the first is still held.

That is not merely wasteful. asyncpg's `Pool.acquire()` defaults to `timeout=None` and its
`_acquire_impl` awaits `self._queue.get()` — it waits FOREVER rather than raising. With the pool at
`max_size` (50 per worker, database.py), enough concurrent requests sitting on that line means every
holder is waiting for a connection only another holder can release: the worker's pool is
permanently deadlocked and every later request on it hangs until the process is killed.

`add_samples` had exactly this — `existing_stats = await db.fetch(...)` inside the ingest
transaction. The fix is always the same one
word: use the `conn` already in hand.

The scan is AST-based on purpose. The first version matched source text and was silently defeated by
a *comment* that quoted `db.pool.acquire()` — it re-opened a phantom block and the real offender
stopped being reported. Comments and strings must not be able to steer this.
"""

import ast
import os
import textwrap

BACKEND = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKIP_DIRS = {"__pycache__", "tests", "db"}

# The Database helper methods — each opens its own pool connection.
_DB_HELPERS = {"fetch", "fetchrow", "fetchval", "execute", "executemany"}


def _opens_pool_connection(node: ast.AST) -> bool:
    """True for `with/async with <anything>.pool.acquire() ...`."""
    for item in getattr(node, "items", []):
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "acquire":
            inner = func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "pool":
                return True
    return False


def _is_db_helper_call(node: ast.AST) -> bool:
    """True for `db.fetch(...)` and friends — the module-level Database singleton."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _DB_HELPERS
        and isinstance(func.value, ast.Name)
        and func.value.id == "db"
    )


def _offenders(root: str) -> list[str]:
    """'path:line: db.<helper>' for every Database-helper call inside a pool.acquire() block."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path) as fh:
                try:
                    tree = ast.parse(fh.read(), filename=path)
                except SyntaxError:  # pragma: no cover - the lint job owns syntax
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.With, ast.AsyncWith)) or not _opens_pool_connection(node):
                    continue
                for stmt in node.body:
                    for inner in ast.walk(stmt):
                        if not isinstance(inner, ast.Call) or not _is_db_helper_call(inner):
                            continue
                        helper = inner.func.attr if isinstance(inner.func, ast.Attribute) else "?"
                        rel = os.path.relpath(path, os.path.dirname(root))
                        found.append(
                            f"{rel}:{inner.lineno}: db.{helper}(...) (block opens at line {node.lineno})",
                        )
    return sorted(found)


def test_no_nested_pool_acquire():
    offenders = _offenders(BACKEND)
    assert not offenders, (
        "A db.* helper is called while a pooled connection is already held. That takes a SECOND "
        "connection from the same pool and can deadlock the worker — asyncpg's acquire() waits "
        "forever. Use the `conn` already in hand:\n  " + "\n  ".join(offenders)
    )


def _scan_source(tmp_path, src: str) -> list[str]:
    path = tmp_path / "sample.py"
    path.write_text(textwrap.dedent(src))
    return _offenders(str(tmp_path))


def test_scan_detects_a_nested_call(tmp_path):
    """Meta-test: a green run has to mean something, so prove the scanner still catches this."""
    hits = _scan_source(
        tmp_path,
        """
        async def handler():
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    rows = await db.fetch('SELECT 1')
            return rows
        """,
    )
    assert len(hits) == 1 and "db.fetch" in hits[0], hits


def test_scan_is_not_fooled_by_comments_or_strings(tmp_path):
    """The regression that broke the first version of this scanner.

    A comment (or SQL string) quoting `db.pool.acquire()` must not open a phantom block, and must
    not stop a real offender from being reported.
    """
    hits = _scan_source(
        tmp_path,
        '''
        async def handler():
            async with db.pool.acquire() as conn:
                # NOTE: we are inside `async with db.pool.acquire() as conn`, so use conn.
                await conn.execute("""
        SELECT 1 -- db.pool.acquire() mentioned in SQL too
        """)
                rows = await db.fetch('SELECT 2')
            return rows
        ''',
    )
    assert len(hits) == 1 and "db.fetch" in hits[0], hits


def test_scan_ignores_a_correctly_scoped_call(tmp_path):
    """A db.* call AFTER the block closes is fine and must not be flagged."""
    hits = _scan_source(
        tmp_path,
        """
        async def handler():
            async with db.pool.acquire() as conn:
                await conn.execute('SELECT 1')
            rows = await db.fetch('SELECT 2')
            return rows
        """,
    )
    assert hits == [], hits
