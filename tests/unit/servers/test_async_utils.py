"""Tests for server async utility helpers."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

import anyio
import pytest

from src.mcp_atlassian.servers.async_utils import (
    BITBUCKET_FETCHER_MAX_WORKERS_ENV,
    DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS,
    DEFAULT_JIRA_FETCHER_MAX_WORKERS,
    JIRA_FETCHER_MAX_WORKERS_ENV,
    get_bitbucket_fetcher_max_workers,
    get_fetcher_max_workers,
    get_jira_fetcher_max_workers,
    run_bitbucket_fetcher_call,
    run_fetcher_call,
    run_jira_fetcher_call,
)


@pytest.fixture(autouse=True)
def reset_fetcher_workers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the per-service worker limit env vars between tests."""
    for env_name in (JIRA_FETCHER_MAX_WORKERS_ENV, BITBUCKET_FETCHER_MAX_WORKERS_ENV):
        monkeypatch.delenv(env_name, raising=False)
    yield
    for env_name in (JIRA_FETCHER_MAX_WORKERS_ENV, BITBUCKET_FETCHER_MAX_WORKERS_ENV):
        monkeypatch.delenv(env_name, raising=False)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, DEFAULT_JIRA_FETCHER_MAX_WORKERS),
        ("1", 1),
        ("8", 8),
        ("16", 16),
        ("0", DEFAULT_JIRA_FETCHER_MAX_WORKERS),
        ("-1", DEFAULT_JIRA_FETCHER_MAX_WORKERS),
        ("abc", DEFAULT_JIRA_FETCHER_MAX_WORKERS),
        ("", DEFAULT_JIRA_FETCHER_MAX_WORKERS),
    ],
)
def test_get_jira_fetcher_max_workers(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: int,
) -> None:
    """Jira worker limit falls back unless env var is a positive integer."""
    if raw_value is None:
        monkeypatch.delenv(JIRA_FETCHER_MAX_WORKERS_ENV, raising=False)
    else:
        monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, raw_value)

    assert get_jira_fetcher_max_workers() == expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS),
        ("3", 3),
        ("0", DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS),
        ("abc", DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS),
    ],
)
def test_get_bitbucket_fetcher_max_workers(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: int,
) -> None:
    """Bitbucket reads its own env var with the same fallback rule as Jira."""
    if raw_value is None:
        monkeypatch.delenv(BITBUCKET_FETCHER_MAX_WORKERS_ENV, raising=False)
    else:
        monkeypatch.setenv(BITBUCKET_FETCHER_MAX_WORKERS_ENV, raw_value)

    assert get_bitbucket_fetcher_max_workers() == expected


def test_invalid_worker_value_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-integer value is reported once and falls back to the default."""
    from src.mcp_atlassian.servers import async_utils

    monkeypatch.setattr(async_utils, "_warned_env_names", set())
    monkeypatch.setenv(BITBUCKET_FETCHER_MAX_WORKERS_ENV, "8.0")

    with caplog.at_level("WARNING", logger="mcp-atlassian.servers.async_utils"):
        first = get_bitbucket_fetcher_max_workers()
        second = get_bitbucket_fetcher_max_workers()

    assert first == second == DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS
    warnings = [
        r for r in caplog.records if BITBUCKET_FETCHER_MAX_WORKERS_ENV in r.message
    ]
    assert len(warnings) == 1
    assert "'8.0'" in warnings[0].message


def test_service_limits_are_read_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One service's env var has no effect on the other service's limit."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "3")

    assert get_fetcher_max_workers("jira") == 3
    assert get_fetcher_max_workers("bitbucket") == DEFAULT_BITBUCKET_FETCHER_MAX_WORKERS


def test_unknown_service_is_rejected() -> None:
    """A service without a registered env var raises instead of sharing a limit."""
    with pytest.raises(ValueError, match="No fetcher worker limit .* 'confluence'"):
        get_fetcher_max_workers("confluence")


@pytest.mark.anyio
async def test_unknown_service_call_is_rejected_before_running() -> None:
    """run_fetcher_call refuses an unknown service without invoking the callable."""
    calls: list[int] = []

    with pytest.raises(ValueError, match="No fetcher worker limit"):
        await run_fetcher_call("confluence", calls.append, 1)

    assert calls == []


@pytest.mark.anyio
async def test_services_hold_separate_limiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated Jira limiter does not delay Bitbucket calls."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "1")
    monkeypatch.setenv(BITBUCKET_FETCHER_MAX_WORKERS_ENV, "2")
    active: dict[str, int] = {"jira": 0, "bitbucket": 0}
    max_active: dict[str, int] = {"jira": 0, "bitbucket": 0}
    lock = Lock()

    def make_blocking_call(service: str):
        def blocking_call(value: int) -> int:
            with lock:
                active[service] += 1
                max_active[service] = max(max_active[service], active[service])
            time.sleep(0.05)
            with lock:
                active[service] -= 1
            return value

        return blocking_call

    async with anyio.create_task_group() as task_group:
        for value in range(3):
            task_group.start_soon(
                run_jira_fetcher_call, make_blocking_call("jira"), value
            )
            task_group.start_soon(
                run_bitbucket_fetcher_call, make_blocking_call("bitbucket"), value
            )

    assert max_active["jira"] == 1
    assert 1 < max_active["bitbucket"] <= 2


@pytest.mark.anyio
async def test_run_jira_fetcher_call_allows_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking Jira calls can overlap while respecting the configured limit."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "2")
    active = 0
    max_active = 0
    lock = Lock()

    def blocking_call(value: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return value

    results: list[int] = []

    async def call(value: int) -> None:
        results.append(await run_jira_fetcher_call(blocking_call, value))

    async with anyio.create_task_group() as task_group:
        for value in range(4):
            task_group.start_soon(call, value)

    assert sorted(results) == [0, 1, 2, 3]
    assert max_active > 1
    assert max_active <= 2


@pytest.mark.anyio
async def test_run_jira_fetcher_call_respects_single_worker_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured single-worker limit serializes offloaded blocking calls."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "1")
    active = 0
    max_active = 0
    lock = Lock()

    def blocking_call(value: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    async with anyio.create_task_group() as task_group:
        for value in range(3):
            task_group.start_soon(run_jira_fetcher_call, blocking_call, value)

    assert max_active == 1


def test_run_jira_fetcher_call_works_from_foreign_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limiter creation is safe when the first call happens outside the main thread."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "2")

    def run_from_worker() -> int:
        return anyio.run(run_jira_fetcher_call, lambda: 42)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future: Future[int] = executor.submit(run_from_worker)
        assert future.result(timeout=5) == 42


def test_run_jira_fetcher_call_works_across_anyio_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limiter state is isolated between asyncio and Trio event loops."""
    monkeypatch.setenv(JIRA_FETCHER_MAX_WORKERS_ENV, "2")

    async def call(value: str) -> str:
        return await run_jira_fetcher_call(lambda: value)

    assert anyio.run(call, "asyncio") == "asyncio"
    assert anyio.run(call, "trio", backend="trio") == "trio"
