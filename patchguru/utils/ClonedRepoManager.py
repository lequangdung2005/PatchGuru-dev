# This file is developed based on the code from the [Testora](https://github.com/michaelpradel/Testora) project by Michael Pradel.
from dataclasses import dataclass
import json
import fcntl
import time
import uuid
from os.path import exists
from typing import Dict, List, Optional, TextIO
from git import Repo
from patchguru.utils.PythonLanguageServer import PythonLanguageServer


@dataclass
class ClonedRepo:
    """A dual-version checked-out repository pair together with its Docker container name.

    Each clone slot holds *two* git checkouts -- the pre-PR commit under
    ``pre_version/`` and the post-PR commit under ``post_version/`` -- so both
    package versions are available side by side for dual (pre/post) execution.
    """

    pre_repo: Repo
    post_repo: Repo
    container_name: str
    pre_language_server: PythonLanguageServer
    post_language_server: PythonLanguageServer

    @property
    def repo(self) -> Repo:
        """Backward-compatible alias for :attr:`pre_repo`."""
        return self.pre_repo

    @property
    def language_server(self) -> PythonLanguageServer:
        """Backward-compatible alias for :attr:`pre_language_server`."""
        return self.pre_language_server


class ClonedRepoManager:
    """Manages a fixed-size pool of pre-cloned repository copies.

    Each clone slot now holds **two** working trees checked out simultaneously:

        <pool_dir>/
            clone1/pre_version/<repo_name>/
            clone1/post_version/<repo_name>/
            clone2/pre_version/<repo_name>/
            clone2/post_version/<repo_name>/
            clone3/pre_version/<repo_name>/
            clone3/post_version/<repo_name>/

    Callers claim a slot for the duration of a dual (pre/post) checkout via
    :meth:`acquire_clone_lease` / :meth:`release_clone_lease`, which take an
    exclusive ``fcntl.flock`` on a per-clone lock file so concurrent processes
    (e.g. multiple analysis runs sharing the same clone pool) never race on the
    same working trees. :meth:`get_cloned_repo` still works without a lease for
    short, read-mostly callers, but the returned slot is not pinned in that case.
    """

    nb_clones = 3

    # NOTE: these are not (yet) sourced from a shared config registry -- a
    # parallel workstream is adding one for the clone-pool tunables. Inlined
    # here for now; move into Config.py once that lands.
    _lock_wait_timeout_seconds = 30.0
    _lock_retry_interval_seconds = 0.1

    def __init__(self, pool_dir, repo_name, repo_id, container_base_name, module_name):
        self.pool_dir = pool_dir
        self.repo_name = repo_name
        self.repo_id = repo_id
        self.container_base_name = container_base_name
        self.module_name = module_name

        self.clone_state_file = f"{self.pool_dir}/clone_state_{repo_name}.json"
        self._read_clone_state()

        self.usage_order: List[str] = [f"clone{i}" for i in range(
            1, self.nb_clones + 1)]  # last = last used

        # Per-clone lock file paths backing the lease API (fcntl.flock-based).
        self._clone_lock_paths: Dict[str, str] = {
            f"clone{i}": f"{self.pool_dir}/clone{i}/.lock" for i in range(1, self.nb_clones + 1)
        }

        # Lease bookkeeping is purely in-memory (not persisted to disk),
        # mirroring the reference implementation in patchguru4py.
        self._lease_token_to_clone_id: Dict[str, str] = {}
        self._lease_token_to_lock_handle: Dict[str, TextIO] = {}
        self._clone_id_to_lease_token: Dict[str, str] = {}

        # NOTE: clones are NOT reset here. Cleanup is deferred and happens
        # lazily, per-clone, under that clone's lock right before it is checked
        # out (see _reset_clone in _checkout_clone_pair). Resetting all 3 clones
        # eagerly at every process start would (a) waste time/network on clones
        # a single run never touches, and (b) make concurrent runs block on the
        # first clone's lock instead of claiming the free ones.

        # start one language server per clone, per side (pre_version/post_version)
        self.clone_id_to_pre_language_server: Dict[str, PythonLanguageServer] = {}
        self.clone_id_to_post_language_server: Dict[str, PythonLanguageServer] = {}
        for i in range(1, self.nb_clones + 1):
            clone_id = f"clone{i}"
            pre_repo_dir = f"{self.pool_dir}/{clone_id}/pre_version/{self.repo_name}"
            post_repo_dir = f"{self.pool_dir}/{clone_id}/post_version/{self.repo_name}"
            if not exists(pre_repo_dir):
                raise FileNotFoundError(
                    f"Pre-version clone directory {pre_repo_dir} does not exist.")
            if not exists(post_repo_dir):
                raise FileNotFoundError(
                    f"Post-version clone directory {post_repo_dir} does not exist.")
            self.clone_id_to_pre_language_server[clone_id] = PythonLanguageServer(pre_repo_dir)
            self.clone_id_to_post_language_server[clone_id] = PythonLanguageServer(post_repo_dir)

    def _read_clone_state(self):
        if not exists(self.clone_state_file):
            self.clone_id_to_state = {
                f"clone{i}": {
                    "pre_commit": "unknown",
                    "post_commit": "unknown",
                    "container_name": f"{self.container_base_name}{i}",
                } for i in range(1, self.nb_clones + 1)}
            return

        with open(self.clone_state_file, "r") as f:
            self.clone_id_to_state = json.load(f)

        assert len(self.clone_id_to_state) == self.nb_clones

        # Migrate legacy-format entries. Older state files stored a single
        # "commit" per clone (pre dual pre/post checkout); a lone commit cannot
        # fill both sides, so reset such entries to "unknown" and let the next
        # checkout repopulate them. Keep the container_name when present.
        for clone_id, state in self.clone_id_to_state.items():
            if "pre_commit" not in state or "post_commit" not in state:
                state["pre_commit"] = "unknown"
                state["post_commit"] = "unknown"
            if "container_name" not in state:
                state["container_name"] = (
                    f"{self.container_base_name}{clone_id.replace('clone', '')}"
                )

    def _write_clone_state(self):
        assert len(self.clone_id_to_state) == self.nb_clones
        with open(self.clone_state_file, "w") as f:
            json.dump(self.clone_id_to_state, f)

    def _reset_clone(self, clone_id: str) -> None:
        """Reset a single clone's two working trees (pre/post) to a clean state.

        Intended to be called from ``_checkout_clone_pair`` while the caller
        already holds the per-clone file lock for *clone_id*, so no lock is
        acquired here. No ``git fetch`` either: missing/failed refs are
        fetched on demand by ``_safe_checkout``'s failure path.
        """
        for version_dir in ("pre_version", "post_version"):
            cloned_repo_dir = f"{self.pool_dir}/{clone_id}/{version_dir}/{self.repo_name}"
            cloned_repo = Repo(cloned_repo_dir)
            cloned_repo.git.rm('--cached', '-rf', '.')
            cloned_repo.git.reset('--hard')
            cloned_repo.git.clean('-f', '-d')

    # ------------------------------------------------------------------
    # Locking helpers
    # ------------------------------------------------------------------

    def _try_acquire_clone_lock(self, clone_id: str) -> Optional[TextIO]:
        """Non-blocking attempt to take the per-clone lock; ``None`` if already held."""
        fh = open(self._clone_lock_paths[clone_id], "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            fh.close()
            return None

    def _release_lock_handle(self, lock_handle: TextIO) -> None:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        finally:
            lock_handle.close()

    # ------------------------------------------------------------------
    # Clone-slot bookkeeping
    # ------------------------------------------------------------------

    def _have_used_clone_id(self, clone_id: str):
        self.usage_order.remove(clone_id)
        self.usage_order.append(clone_id)

    def _unleased_usage_order(self) -> List[str]:
        """LRU-ordered clone ids, excluding any currently held via a lease."""
        return [cid for cid in self.usage_order if cid not in self._clone_id_to_lease_token]

    def _safe_checkout(self, cloned_repo: Repo, commit: str):
        try:
            cloned_repo.git.checkout(commit)
            cloned_repo.git.submodule('update', '--init', '--recursive')
        except Exception as e:
            if commit == "main":
                self._safe_checkout(cloned_repo, "master")
            elif commit == "master":
                self._safe_checkout(cloned_repo, "dev")
            else:
                cloned_repo.git.rm('--cached', '-rf', '.')
                cloned_repo.git.reset('--hard')
                cloned_repo.git.clean('-f', '-d')
                origin = cloned_repo.remotes.origin
                origin.fetch()
                cloned_repo.git.checkout(commit)

    def _build_cloned_repo(self, clone_id: str) -> ClonedRepo:
        pre_repo_dir = f"{self.pool_dir}/{clone_id}/pre_version/{self.repo_name}"
        post_repo_dir = f"{self.pool_dir}/{clone_id}/post_version/{self.repo_name}"
        return ClonedRepo(
            Repo(pre_repo_dir),
            Repo(post_repo_dir),
            self.clone_id_to_state[clone_id]["container_name"],
            self.clone_id_to_pre_language_server[clone_id],
            self.clone_id_to_post_language_server[clone_id],
        )

    def _checkout_clone_pair(self, clone_id: str, pre_commit: str, post_commit: str) -> ClonedRepo:
        """Check out *pre_commit*/*post_commit* into *clone_id*'s two working trees.

        Assumes the per-clone file lock for *clone_id* is already held by the caller.
        """
        pre_repo_dir = f"{self.pool_dir}/{clone_id}/pre_version/{self.repo_name}"
        post_repo_dir = f"{self.pool_dir}/{clone_id}/post_version/{self.repo_name}"
        pre_repo = Repo(pre_repo_dir)
        post_repo = Repo(post_repo_dir)
        self._reset_clone(clone_id)
        self._safe_checkout(pre_repo, pre_commit)
        self._safe_checkout(post_repo, post_commit)

        # update clone state
        state = self.clone_id_to_state[clone_id]
        state["pre_commit"] = pre_commit
        state["post_commit"] = post_commit
        self.clone_id_to_state[clone_id] = state
        self._write_clone_state()

        time.sleep(1)

        return self._build_cloned_repo(clone_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire_clone_lease(self, pre_commit: str, post_commit: str) -> str:
        """Lease a clone slot checked out at ``(pre_commit, post_commit)``.

        Blocks (subject to :attr:`_lock_wait_timeout_seconds`) until a slot's
        per-clone file lock can be taken, then performs the dual checkout and
        holds the lock until :meth:`release_clone_lease` is called with the
        returned token.

        Returns:
            An opaque lease token to pass to :meth:`get_cloned_repo` /
            :meth:`release_clone_lease`.

        Raises:
            RuntimeError: If no slot could be leased before the timeout.
        """
        lease_token = uuid.uuid4().hex
        deadline = time.monotonic() + self._lock_wait_timeout_seconds
        while True:
            # Prefer a slot already checked out to the requested pair, then LRU order.
            matching = [
                cid for cid, state in self.clone_id_to_state.items()
                if state["pre_commit"] == pre_commit and state["post_commit"] == post_commit
                and cid not in self._clone_id_to_lease_token
            ]
            candidates = matching + [
                cid for cid in self._unleased_usage_order() if cid not in matching
            ]
            for clone_id in candidates:
                lock_handle = self._try_acquire_clone_lock(clone_id)
                if lock_handle is None:
                    continue
                keep_lock = False
                try:
                    self._checkout_clone_pair(clone_id, pre_commit, post_commit)
                    self._have_used_clone_id(clone_id)
                    self._lease_token_to_clone_id[lease_token] = clone_id
                    self._lease_token_to_lock_handle[lease_token] = lock_handle
                    self._clone_id_to_lease_token[clone_id] = lease_token
                    keep_lock = True
                    return lease_token
                finally:
                    if not keep_lock:
                        self._release_lock_handle(lock_handle)
            if time.monotonic() >= deadline:
                break
            time.sleep(self._lock_retry_interval_seconds)
        raise RuntimeError(
            "No clone slots available to lease; all slots are busy or already leased.")

    def release_clone_lease(self, lease_token: str) -> None:
        """Release a previously acquired clone lease token."""
        clone_id = self._lease_token_to_clone_id.pop(lease_token, None)
        lock_handle = self._lease_token_to_lock_handle.pop(lease_token, None)
        if clone_id is not None:
            self._clone_id_to_lease_token.pop(clone_id, None)
        if lock_handle is not None:
            self._release_lock_handle(lock_handle)

    def get_cloned_repo(
        self, pre_commit: str, post_commit: str, lease_token: Optional[str] = None
    ) -> ClonedRepo:
        """Return a clone pair checked out at ``(pre_commit, post_commit)``.

        When *lease_token* is provided, returns the leased slot associated with
        that token and validates that it is still checked out at the requested
        commit pair.

        Without a lease token, the slot lock is held only for the duration of
        the checkout and released before returning -- the returned
        :class:`ClonedRepo` is *not* pinned, so a concurrent lease/checkout
        elsewhere could re-check-out the same slot afterwards. Callers that run
        commands against the result over a longer window should hold a lease
        (:meth:`acquire_clone_lease`) instead.
        """
        if lease_token is not None:
            clone_id = self._lease_token_to_clone_id.get(lease_token)
            if clone_id is None:
                raise RuntimeError(f"Unknown lease token: {lease_token}")
            state = self.clone_id_to_state[clone_id]
            if state["pre_commit"] != pre_commit or state["post_commit"] != post_commit:
                raise RuntimeError(
                    f"Lease token {lease_token} is pinned to "
                    f"({state['pre_commit']}, {state['post_commit']}) but "
                    f"({pre_commit}, {post_commit}) was requested.")
            return self._build_cloned_repo(clone_id)

        # reuse an existing (unleased) clone if it already matches
        for clone_id, state in self.clone_id_to_state.items():
            if (state["pre_commit"] == pre_commit and state["post_commit"] == post_commit
                    and clone_id not in self._clone_id_to_lease_token):
                self._have_used_clone_id(clone_id)
                return self._build_cloned_repo(clone_id)

        # checkout desired commits on an unleased slot, LRU-first
        deadline = time.monotonic() + self._lock_wait_timeout_seconds
        while True:
            for clone_id in self._unleased_usage_order():
                lock_handle = self._try_acquire_clone_lock(clone_id)
                if lock_handle is None:
                    continue
                try:
                    cloned_repo = self._checkout_clone_pair(clone_id, pre_commit, post_commit)
                    self._have_used_clone_id(clone_id)
                    return cloned_repo
                finally:
                    self._release_lock_handle(lock_handle)
            if time.monotonic() >= deadline:
                break
            time.sleep(self._lock_retry_interval_seconds)
        raise RuntimeError(
            "No clone slots available; all slots are currently busy or leased.")
