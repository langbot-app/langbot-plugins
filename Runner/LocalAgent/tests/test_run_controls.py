"""Regression tests for local run controls, using genuine SDK contexts."""

from __future__ import annotations

import asyncio

import pytest

from components.runner.default import RunDeadline, RunInterruptChecker
from tests.test_runner import make_context


@pytest.mark.asyncio
async def test_slow_ledger_does_not_delay_chunks_or_deadline():
    calls = 0

    class SlowLedger:
        async def run_get(self, run_id):
            nonlocal calls
            calls += 1
            await asyncio.sleep(10)

    context = make_context()
    context.context.available_apis.run_get = True
    async with RunInterruptChecker(SlowLedger(), context) as checker:
        for _ in range(200):
            assert await checker.wait_for(asyncio.sleep(0, result="chunk"), deadline=RunDeadline(0.1)) == "chunk"
        assert calls == 1
        with pytest.raises(asyncio.TimeoutError):
            await checker.wait_for(asyncio.sleep(10), deadline=RunDeadline(0.02))
    assert checker._watcher is None


@pytest.mark.asyncio
async def test_operation_timeout_is_not_mistaken_for_poll_timeout():
    checker = RunInterruptChecker(None, make_context())
    operation_error = asyncio.TimeoutError("operation itself timed out")

    async def operation():
        raise operation_error

    with pytest.raises(asyncio.TimeoutError) as caught:
        await checker.wait_for(operation(), deadline=RunDeadline(0.02))

    assert caught.value is operation_error


@pytest.mark.asyncio
async def test_operation_timeout_propagates_without_deadline_when_polling():
    class RunLedgerFixture:
        async def run_get(self, run_id):
            # Give the enclosing test timeout a chance to stop a regression loop.
            await asyncio.sleep(0)
            return {"run_id": run_id, "status": "running"}

    context = make_context()
    context.context.available_apis.run_get = True
    checker = RunInterruptChecker(RunLedgerFixture(), context)
    operation_error = asyncio.TimeoutError("operation itself timed out")

    async def operation():
        raise operation_error

    with pytest.raises(asyncio.TimeoutError) as caught:
        await asyncio.wait_for(checker.wait_for(operation()), timeout=0.25)

    assert caught.value is operation_error
