"""Pull-based build system inspired by Shake.

Provides a generic Build class for defining and executing tasks with
dependencies. Tasks are built on-demand, with parallel execution and
proper error propagation.
"""

from __future__ import annotations

import fnmatch
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

logger = logging.getLogger(__name__)

RuleFn = Callable[[str], None]


class Build:
    """A pull-based build system inspired by Shake.

    Targets are built on-demand via need(). Dependencies are declared inline
    by calling need() within a rule. The system handles parallel execution
    while respecting dependencies.

    Thread safety:
    - _done, _failed, _building are protected by _lock
    - Multiple threads may call need() concurrently
    - Each target is built exactly once (success) or marked failed permanently
    """

    def __init__(self):
        self._rules: list[tuple[str, RuleFn]] = []
        # Targets currently being built, maps to their Future
        self._building: dict[str, Future[None]] = {}
        # Successfully completed targets
        self._done: set[str] = set()
        # Failed targets with their exceptions (permanent, no retry)
        self._failed: dict[str, BaseException] = {}
        self._lock: Lock = Lock()
        self._executor: ThreadPoolExecutor | None = None

    def rule(self, pattern: str) -> Callable[[RuleFn], RuleFn]:
        """Register a rule.

        @build.rule("fetch_feeds")
        def _(...): ...

        @build.rule("render_page:*")
        def _(...): ...  # pattern with wildcard
        """

        def decorator(fn: RuleFn) -> RuleFn:
            self._rules.append((pattern, fn))
            return fn

        return decorator

    def _find_rule(self, target: str) -> RuleFn:
        for pattern, fn in self._rules:
            if fnmatch.fnmatch(target, pattern):
                return fn
        raise ValueError(f"No rule matches target: {target}")

    def need(self, *targets: str) -> None:
        """Demand that targets are built. Blocks until complete.

        For each target:
        1. If already failed -> raise stored exception immediately
        2. If already done -> skip
        3. If currently building -> wait on existing Future
        4. Otherwise -> submit new build task

        Raises:
            The exception from any failed target (either stored or from Future).
        """
        assert self._executor is not None
        futures: list[Future[None]] = []

        for target in targets:
            future: Future[None] | None = None
            rule: RuleFn | None = None

            with self._lock:
                if target in self._failed:
                    raise self._failed[target]
                if target in self._done:
                    continue
                elif target in self._building:
                    future = self._building[target]
                else:
                    # We're the first to build this target
                    rule = self._find_rule(target)
                    future = self._executor.submit(self._build_target, target, rule)
                    self._building[target] = future

            if future is not None:
                logger.debug(f"Waiting on: {target}")
                futures.append(future)

        # Wait for all targets to complete; propagates first exception
        for f in futures:
            f.result()

    def _build_target(self, target: str, rule: RuleFn) -> None:
        """Execute a rule and update target state atomically.

        On success: target added to _done
        On failure: target added to _failed with exception (permanent)
        Always: target removed from _building

        The exception is captured and stored before re-raising so that
        other threads waiting on this target (or future need() calls)
        see a consistent failure state.
        """
        logger.debug(f"Building: {target}")
        exc: BaseException | None = None
        try:
            rule(target)
        except BaseException as e:
            exc = e
            raise
        finally:
            # Atomic state update: either done or failed, never both
            with self._lock:
                if exc is None:
                    self._done.add(target)
                else:
                    self._failed[target] = exc
                    logger.debug(f"Failed: {target}")
                if target in self._building:
                    del self._building[target]
            if exc is None:
                logger.debug(f"Completed: {target}")

    def run(self, target: str) -> None:
        """Build a target and all its dependencies."""
        with ThreadPoolExecutor() as executor:
            self._executor = executor
            self.need(target)
