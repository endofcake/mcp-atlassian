"""Async helpers for server tool implementations.

The fetchers are synchronous ``requests`` clients. A tool that calls one
directly inside ``async def`` blocks the event loop for the duration of the
upstream round trip, which stalls every other connected client. The helpers
here run such calls in a worker thread behind a per-service capacity limiter,
so each service has its own bound on concurrent upstream calls and a slow
instance of one service cannot exhaust the threads of another.

A cancelled tool call waits for its worker to finish. The blocking call
cannot be interrupted once it has started, so the thread and its limiter slot
stay held until the fetcher's request timeout fires, and a write that has
already reached the upstream completes there.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

import anyio
from anyio.lowlevel import RunVar

logger = logging.getLogger("mcp-atlassian.servers.async_utils")

P = ParamSpec("P")
T = TypeVar("T")

DEFAULT_FETCHER_MAX_WORKERS = 8

JIRA_FETCHER_MAX_WORKERS_ENV = "JIRA_FETCHER_MAX_WORKERS"
BITBUCKET_FETCHER_MAX_WORKERS_ENV = "BITBUCKET_FETCHER_MAX_WORKERS"
DEFAULT_JIRA_FETCHER_MAX_WORKERS = DEFAULT_FETCHER_MAX_WORKERS
DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS = DEFAULT_FETCHER_MAX_WORKERS

FETCHER_MAX_WORKERS_ENV: dict[str, str] = {
    "jira": JIRA_FETCHER_MAX_WORKERS_ENV,
    "bitbucket": BITBUCKET_FETCHER_MAX_WORKERS_ENV,
}

_fetcher_limiters: RunVar[dict[str, tuple[int, anyio.CapacityLimiter]] | None] = RunVar(
    "fetcher_limiters", default=None
)
_warned_env_names: set[str] = set()


def _fetcher_env_name(service: str) -> str:
    """Return the worker-limit env var for a service, rejecting unknown names."""
    try:
        return FETCHER_MAX_WORKERS_ENV[service]
    except KeyError:
        known = ", ".join(sorted(FETCHER_MAX_WORKERS_ENV))
        raise ValueError(
            f"No fetcher worker limit is defined for service {service!r}; "
            f"known services: {known}."
        ) from None


def get_fetcher_max_workers(service: str) -> int:
    """Return the configured maximum concurrent fetcher calls for a service.

    The value comes from the service's env var. A missing value falls back to
    ``DEFAULT_FETCHER_MAX_WORKERS`` silently; a non-integer or non-positive
    value falls back with one warning per env var, so a misconfiguration is
    visible in the log.
    """
    env_name = _fetcher_env_name(service)
    raw_value = os.getenv(env_name)
    if not raw_value:
        return DEFAULT_FETCHER_MAX_WORKERS

    try:
        worker_count = int(raw_value)
    except ValueError:
        worker_count = 0

    if worker_count <= 0:
        if env_name not in _warned_env_names:
            _warned_env_names.add(env_name)
            logger.warning(
                "%s=%r is not a positive integer; using the default of %d",
                env_name,
                raw_value,
                DEFAULT_FETCHER_MAX_WORKERS,
            )
        return DEFAULT_FETCHER_MAX_WORKERS
    return worker_count


def get_jira_fetcher_max_workers() -> int:
    """Return the configured maximum concurrent Jira fetcher calls."""
    return get_fetcher_max_workers("jira")


def get_bitbucket_fetcher_max_workers() -> int:
    """Return the configured maximum concurrent Bitbucket fetcher calls."""
    return get_fetcher_max_workers("bitbucket")


def _get_fetcher_limiter(service: str) -> anyio.CapacityLimiter:
    """Return an event-loop-local limiter for one service's worker threads.

    Limiters live in a ``RunVar`` so each event loop (and each anyio backend)
    owns its own set. A limiter is rebuilt when the configured worker count
    changes between calls.
    """
    worker_count = get_fetcher_max_workers(service)
    limiters = _fetcher_limiters.get()
    if limiters is None:
        limiters = {}
        _fetcher_limiters.set(limiters)
    limiter_state = limiters.get(service)
    if limiter_state is None or limiter_state[0] != worker_count:
        limiter_state = (worker_count, anyio.CapacityLimiter(worker_count))
        limiters[service] = limiter_state
    return limiter_state[1]


async def run_fetcher_call(
    service: str,
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run a blocking fetcher call in a worker thread bounded per service."""
    call = partial(func, *args, **kwargs)
    return await anyio.to_thread.run_sync(
        call,
        limiter=_get_fetcher_limiter(service),
    )


async def run_jira_fetcher_call(
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run a blocking Jira fetcher call in a bounded worker thread."""
    return await run_fetcher_call("jira", func, *args, **kwargs)


async def run_bitbucket_fetcher_call(
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run a blocking Bitbucket fetcher call in a bounded worker thread."""
    return await run_fetcher_call("bitbucket", func, *args, **kwargs)
