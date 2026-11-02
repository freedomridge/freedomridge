"""Oldest-access-first cache reconciliation, used only by background maintenance."""

import os
import time


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def prune(dirs, budget) -> int:
    files = []
    total = 0
    stale_tmp_before = time.time() - 3600
    for root in dict.fromkeys(os.path.realpath(d) for d in dirs):
        if not os.path.isdir(root):
            continue
        for dirpath, _subdirs, names in os.walk(root):
            for name in names:
                if name == '.cache-budget.lock':
                    continue  # cross-worker lock inode; leave it
                p = os.path.join(dirpath, name)
                try:
                    if os.path.islink(p):
                        continue
                    st = os.stat(p)
                    if name.endswith('.tmp'):
                        if st.st_mtime < stale_tmp_before:
                            os.unlink(p)
                        continue  # protect active writes; only remove hour-old orphans
                except OSError:
                    continue
                files.append((st.st_atime, st.st_size, p))
                total += st.st_size

    if total <= budget:
        print(f"[prune] {_human(total)} <= budget {_human(budget)}; nothing to do")
        return total

    files.sort()  # oldest access time first
    target = total - budget
    freed = 0
    removed = 0
    for _atime, size, p in files:
        if freed >= target:
            break
        try:
            os.unlink(p)
            freed += size
            removed += 1
        except OSError:
            pass
    print(
        f"[prune] {_human(total)} -> {_human(total - freed)} "
        f"(budget {_human(budget)}); removed {removed} files, freed {_human(freed)}",
    )
    return total - freed
