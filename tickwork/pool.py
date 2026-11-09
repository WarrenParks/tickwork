"""Role-keyed flock claim and worker-slot primitives.

The concurrency primitives a worker pool needs, built on ``fcntl.flock`` with
non-blocking acquisition: no waiting, no daemon, and no explicit cleanup (the
lock is released when the holding process exits or crashes).

- ``try_claim(role, state_dir, kind, repo, number)`` — claim exclusive
  ownership of an item (e.g. ``kind="issue"`` or ``"pr"``) so two concurrent
  ticks of *role* never work the same item.
- ``acquire_worker_slot(role, state_dir, pool_size)`` — claim the
  lowest-numbered free slot in ``1..pool_size`` for per-tick disambiguation
  (branch naming, local state).
- ``lock_worker_slot(role, state_dir, slot)`` — claim a *specific* slot's
  lease (no free-slot scan), for when the slot is pinned from outside the
  process.
- ``flock_nonblock(path)`` — open *path* and take a non-blocking exclusive
  ``flock`` on it; returns the file descriptor, or ``None`` when another
  holder has the lock.  Public so a caller can hold an extra lock file
  next to the generic ones.

Lock files live under ``<state_dir>/claims/<role>/<kind>-<owner>_<repo>-<n>.lock``
and ``<state_dir>/slots/<role>/<slot>.lock``.  The state directory is
passed in by the caller; this module reads no environment variables, so it
names nothing of the instance it runs for.

stdlib-only — no new dependencies.  ``fcntl.flock`` is POSIX-only; the module
will fail at import on non-POSIX platforms (consistent with the repo's
single-host Linux deploy model).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Generator


def _item_lock_path(role: str, state_dir: str, kind: str, repo: str | None, number: int) -> str:
    """Full path to the lock file for one item of *kind* held by *role*.

    With *repo*, the path is keyed by repo too, so the same item number in
    two repos doesn't share one lock.  ``owner/name`` is flattened into one
    path component (``owner_name``).
    """
    claims_dir = os.path.join(state_dir, "claims", role)
    if repo:
        return os.path.join(claims_dir, f"{kind}-{repo.replace('/', '_')}-{number}.lock")
    return os.path.join(claims_dir, f"{kind}-{number}.lock")


def _slot_lock_path(role: str, state_dir: str, slot: int) -> str:
    """Full path to the lock file for a worker slot (1-indexed)."""
    return os.path.join(state_dir, "slots", role, f"{slot}.lock")


# ─── helpers ─────────────────────────────────────────────────────────────────


def _ensure_dir(path: str) -> None:
    """Create *path* (and parents) if absent.  Silent on success; raises on
    permission errors so the caller can surface the failure."""
    os.makedirs(path, exist_ok=True)


def flock_nonblock(path: str) -> int | None:
    """Open *path* for writing (creating it if needed) and try a non-blocking
    exclusive ``flock``.  Returns the file descriptor on success, or ``None``
    when another process already holds the lock (``EWOULDBLOCK``).

    The lock file is created with ``os.O_CREAT`` so it persists on disk even
    after the lock is released — this is intentional: ``flock`` is advisory
    and tied to the file descriptor, so the file's existence on disk is
    harmless and re-using the same path is critical for cross-process
    coordination.

    On an unrecoverable OS error (e.g. ``PermissionError``, ``IOError`` from
    a full filesystem) the exception propagates so the caller can escalate
    rather than silently proceeding without a lock.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError as e:
        # The directory doesn't exist yet — create it and retry once.
        if e.errno == 2:  # ENOENT
            _ensure_dir(os.path.dirname(path))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        else:
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (11, 35):  # EAGAIN / EWOULDBLOCK
            return None
        raise
    return fd


@contextlib.contextmanager
def _lock_holder(fd: int) -> Generator[None, None, None]:
    """Context manager that holds a ``flock``-ed file descriptor for the
    duration of the ``with`` block, then releases it.

    The lock is released either by an explicit ``os.close`` (the ``finally``
    block), or automatically when the process exits — the kernel releases all
    file-descriptor locks on process termination, so a crashed/killed process
    never leaves a stale lock behind.
    """
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # best-effort; a crash during yield may leave fd stale, but
            # the kernel will clean up on process exit.
        try:
            os.close(fd)
        except OSError:
            pass


@contextlib.contextmanager
def _slot_id_holder(fd: int, slot: int) -> Generator[int, None, None]:
    """Like ``_lock_holder`` but also yields the *slot* integer to the ``with``
    target so ``acquire_worker_slot`` and ``lock_worker_slot`` can be used as::

        with acquire_worker_slot(role, state, 3) as slot:
            print(slot)  # 1, 2, or 3
    """
    try:
        yield slot
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ─── public API ──────────────────────────────────────────────────────────────


def try_claim(
    role: str, state_dir: str, kind: str, repo: str | None, number: int
) -> contextlib.AbstractContextManager | None:
    """Attempt a non-blocking claim by *role* on item *number* of *kind*.

    *kind* selects the lock-file prefix (``"issue"``, ``"pr"``, …); each kind
    gets its own lock, so a PR 7 and an issue 7 don't collide.  *repo* keys
    the lock by repository when given.

    Returns a context manager that holds the exclusive lock for the lifetime
    of the ``with`` block, or ``None`` if another process already holds the
    claim.  The lock is released on context-manager exit OR on process
    exit/crash (kernel-managed, no cleanup needed).

    Usage::

        claim = try_claim(role, state_dir, "issue", repo, 42)
        if claim is None:
            log("issue 42 already claimed by another tick")
            return
        with claim:
            ...  # compose the work
    """
    fd = flock_nonblock(_item_lock_path(role, state_dir, kind, repo, number))
    if fd is None:
        return None
    return _lock_holder(fd)


def lock_worker_slot(role: str, state_dir: str, slot: int) -> contextlib.AbstractContextManager | None:
    """Claim the flock lease on *specifically* slot *slot* — no free-slot scan.

    Companion to :func:`acquire_worker_slot` for the pinned-slot case: when
    the slot is set from outside the process, the lease must still be *taken*
    so concurrent ticks on the same slot collide instead of silently running
    double — but which slot it is has already been decided, so there is
    nothing to scan for.

    Returns a context manager yielding *slot*, or ``None`` if the slot is
    already held by another worker (caller should skip the tick, same contract
    as a full :func:`acquire_worker_slot` scan).
    """
    if slot is None or slot < 1:
        return None
    fd = flock_nonblock(_slot_lock_path(role, state_dir, slot))
    if fd is None:
        return None
    return _slot_id_holder(fd, slot)


def acquire_worker_slot(
    role: str, state_dir: str, pool_size: int
) -> contextlib.AbstractContextManager | None:
    """Claim the lowest-numbered free worker slot in ``1..pool_size`` for *role*.

    Returns a context manager that yields the slot number (``int``) for the
    duration of the ``with`` block, or ``None`` if every slot is taken (or
    ``pool_size < 1``).  When all slots are occupied, returns ``None``
    immediately — the caller should skip the tick rather than waiting.
    """
    if pool_size < 1:
        return None
    for slot in range(1, pool_size + 1):
        fd = flock_nonblock(_slot_lock_path(role, state_dir, slot))
        if fd is not None:
            return _slot_id_holder(fd, slot)
    return None
