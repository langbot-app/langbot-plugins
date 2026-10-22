from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest
from langbot_plugin.api.entities.builtin.provider.message import ContentElement, FunctionCall, Message, ToolCall
from pydantic import BaseModel

from pkg.context_pipeline import ContextBudget, ContextCompactor, ContextUsageAnchor, HostContextTokenCounter


def payload(messages: list[Message]) -> tuple[str, ...]:
    return tuple(message.model_dump_json(exclude_none=True) for message in messages)


class CountingHost:
    def __init__(self, base_tokens: int = 7):
        self.calls: list[tuple[str, ...]] = []
        self.base_tokens = base_tokens

    async def count_tokens(self, *, messages: list[Message], **kwargs) -> int:
        snapshot = payload(messages)
        self.calls.append(snapshot)
        await asyncio.sleep(0)
        return self.base_tokens + sum(len(message) for message in snapshot)


def compactor(host: CountingHost, **kwargs) -> ContextCompactor:
    return ContextCompactor(ContextBudget(), token_counter=HostContextTokenCounter(host, "model-1"), **kwargs)


def inputs() -> dict:
    return {
        "prompt_messages": [Message(role="system", content="policy")],
        "history_messages": [],
        "current_messages": [Message(role="user", content="hello")],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_tokens", [0, 7])
async def test_one_assembly_counts_each_distinct_payload_once(empty_tokens):
    host = CountingHost(base_tokens=empty_tokens)
    result = await compactor(host).compact_async(**inputs())

    assert len(host.calls) == 4
    assert max(Counter(host.calls).values()) == 1
    assert host.calls.count(()) == 1
    assert result.diagnostics.summary_tokens == empty_tokens
    assert result.diagnostics.history_tokens == empty_tokens
    assert result.diagnostics.rag_tokens == empty_tokens
    assert result.tokens_before == result.tokens_after == empty_tokens + sum(map(len, payload(result.messages)))
    assert [message.content for message in result.messages] == ["policy", "hello"]
    assert result.compacted is False


@pytest.mark.asyncio
async def test_reusing_compactor_starts_a_new_counting_scope():
    host = CountingHost()
    instance = compactor(host)
    first = await instance.compact_async(**inputs())
    second = await instance.compact_async(**inputs())

    assert Counter(host.calls) == {key: 2 for key in set(host.calls)}
    assert len(host.calls) == 8
    assert first == second


@pytest.mark.asyncio
async def test_concurrent_assemblies_on_one_compactor_are_independent():
    host = CountingHost()
    instance = compactor(host)
    results = await asyncio.gather(*(instance.compact_async(**inputs()) for _ in range(3)))

    assert len(host.calls) == 12
    assert set(Counter(host.calls).values()) == {3}
    assert all(result == results[0] for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_length", [0, 1, 2])
async def test_usage_anchor_does_not_replace_the_full_host_count(prefix_length):
    host = CountingHost()
    instance = compactor(
        host, usage_anchor=ContextUsageAnchor(message_count=prefix_length, total_tokens=1000, model_id="model-1")
    )
    result = await instance.compact_async(**inputs())

    assert result.tokens_before == 1000 + 7 + sum(map(len, payload(result.messages[prefix_length:])))
    assert result.tokens_after == 7 + sum(map(len, payload(result.messages)))
    assert result.tokens_after != result.tokens_before
    assert len(host.calls) == 4
    assert max(Counter(host.calls).values()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["role", "image", "file_name", "provider_metadata"])
async def test_single_field_differences_are_counted_separately(difference):
    first = Message(role="user", content="same")
    second = first.model_copy(deep=True)
    if difference == "role":
        second.role = "assistant"
    elif difference == "image":
        first.content = [ContentElement.from_image_base64("b25l")]
        second.content = [ContentElement.from_image_base64("dHdv")]
    elif difference == "file_name":
        first.content = [ContentElement.from_file_base64("c2FtZQ==", "one.txt")]
        second.content = [ContentElement.from_file_base64("c2FtZQ==", "two.txt")]
    else:
        first.provider_specific_fields = {"signature": {"value": "one"}}
        second.provider_specific_fields = {"signature": {"value": "two"}}
    host = CountingHost()
    result = await compactor(host).compact_async(
        prompt_messages=[first], history_messages=[], current_messages=[second]
    )

    assert payload(result.frame.prompt) in host.calls
    assert payload(result.frame.current) in host.calls
    assert result.diagnostics.prompt_tokens == 7 + sum(map(len, payload(result.frame.prompt)))
    assert result.diagnostics.current_tokens == 7 + sum(map(len, payload(result.frame.current)))
    assert len(host.calls) == 4


@pytest.mark.asyncio
async def test_tool_arguments_are_part_of_the_counted_payload():
    def turn(arguments: str) -> list[Message]:
        return [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="call-1", type="function", function=FunctionCall(name="echo", arguments=arguments))
                ],
            ),
            Message(role="tool", tool_call_id="call-1", content="ok"),
        ]

    host = CountingHost()
    result = await compactor(host).compact_async(
        prompt_messages=[], history_messages=turn('{"value":1}'), current_messages=turn('{"value":100}')
    )

    assert payload(result.frame.history) in host.calls
    assert payload(result.frame.current) in host.calls
    assert result.diagnostics.history_tokens != result.diagnostics.current_tokens
    assert [message.role for message in result.messages] == ["assistant", "tool", "assistant", "tool"]
    assert max(Counter(host.calls).values()) == 1


@pytest.mark.asyncio
async def test_compaction_preserves_current_input_and_counts_changed_slices():
    host = CountingHost()
    budget = ContextBudget(window_tokens=600, reserve_tokens=100, keep_recent_tokens=80, summary_tokens=40)
    result = await ContextCompactor(budget, token_counter=HostContextTokenCounter(host, "model-1")).compact_async(
        prompt_messages=[Message(role="system", content="policy")],
        history_messages=[Message(role="user", content=f"history-{index} " + "words " * 40) for index in range(8)],
        rag_messages=[Message(role="user", content="reference " * 30)],
        current_messages=[Message(role="user", content="current request")],
    )

    assert result.compacted is True
    assert result.tokens_before > budget.input_tokens
    assert result.tokens_after <= budget.input_tokens
    assert result.messages[-1].content == "current request"
    assert result.tokens_after == 7 + sum(map(len, payload(result.messages)))
    assert payload(result.messages) in host.calls
    assert max(Counter(host.calls).values()) == 1


@pytest.mark.asyncio
async def test_failed_assembly_does_not_reuse_earlier_successful_counts():
    class FailingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            tokens = await super().count_tokens(messages=messages, **kwargs)
            if len(self.calls) == 2:
                raise RuntimeError("Host temporarily unavailable")
            return tokens

    host = FailingHost()
    instance = compactor(host)
    with pytest.raises(RuntimeError, match="Host temporarily unavailable"):
        await instance.compact_async(**inputs())
    result = await instance.compact_async(**inputs())

    assert Counter(host.calls)[payload(result.messages)] == 2
    assert len(host.calls) == 6


@pytest.mark.asyncio
async def test_cancelled_assembly_does_not_reuse_earlier_successful_counts():
    entered = asyncio.Event()
    released = asyncio.Event()

    class BlockingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            tokens = await super().count_tokens(messages=messages, **kwargs)
            if len(self.calls) == 2:
                entered.set()
                await released.wait()
            return tokens

    host = BlockingHost()
    instance = compactor(host)
    task = asyncio.create_task(instance.compact_async(**inputs()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await instance.compact_async(**inputs())

    assert Counter(host.calls)[payload(result.messages)] == 2
    assert len(host.calls) == 6


@pytest.mark.asyncio
async def test_different_multimodal_payloads_and_provider_fields_are_not_merged():
    host = CountingHost()
    prompt = Message(role="system", content="same")
    current = Message(
        role="user",
        content=[ContentElement.from_text("same"), ContentElement.from_image_base64("aW1hZ2U=")],
    )
    history = [
        Message(role="assistant", content="same", provider_specific_fields={"signature": "one"}),
        Message(
            role="user",
            content=[ContentElement.from_text("same"), ContentElement.from_file_base64("ZmlsZQ==", "a.txt")],
        ),
    ]
    result = await compactor(host).compact_async(
        prompt_messages=[prompt], history_messages=history, current_messages=[current]
    )

    assert max(Counter(host.calls).values()) == 1
    assert payload(result.frame.prompt) in host.calls
    assert payload(result.frame.history) in host.calls
    assert payload(result.frame.current) in host.calls
    assert len({payload(result.frame.prompt), payload(result.frame.history), payload(result.frame.current)}) == 3


@pytest.mark.asyncio
async def test_mutated_messages_are_counted_again_within_one_assembly():
    class MutatingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            tokens = await super().count_tokens(messages=messages, **kwargs)
            if len(self.calls) == 1:
                messages[-1].content = "updated input"
            return tokens

    host = MutatingHost()
    result = await compactor(host).compact_async(**inputs())

    assert result.tokens_before != result.tokens_after
    assert result.messages[-1].content == "updated input"
    assert result.tokens_after == 7 + sum(map(len, payload(result.messages)))
    assert max(Counter(host.calls).values()) == 1


@pytest.mark.asyncio
async def test_sdk_json_strings_are_counted_without_extra_restrictions():
    class SDKEncodingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            snapshot = tuple(json.dumps(message.model_dump()) for message in messages)
            self.calls.append(snapshot)
            return 7 + sum(map(len, snapshot))

    host = SDKEncodingHost()
    current = Message(role="user", content=json.loads('"\\ud800"'))
    result = await compactor(host).compact_async(prompt_messages=[], history_messages=[], current_messages=[current])

    assert result.messages == [current]
    assert result.tokens_before == result.tokens_after == 7 + len(json.dumps(current.model_dump()))
    assert host.calls


class ProviderDetails(BaseModel):
    optional: str | None = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [(None, float("inf")), (ProviderDetails(), {}), (0.0, -0.0), (True, 1)],
    ids=["nonfinite", "nested_model", "signed_zero", "boolean_integer"],
)
async def test_sdk_distinct_provider_values_are_not_normalized_into_one_key(values):
    class SDKEncodingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            snapshot = tuple(json.dumps(message.model_dump()) for message in messages)
            self.calls.append(snapshot)
            return 7 + sum(map(len, snapshot))

    host = SDKEncodingHost()
    first = Message(role="assistant", content="same", provider_specific_fields={"value": values[0]})
    second = Message(role="assistant", content="same", provider_specific_fields={"value": values[1]})
    result = await compactor(host).compact_async(
        prompt_messages=[first], history_messages=[], current_messages=[second]
    )

    assert (json.dumps(first.model_dump()),) in host.calls
    assert (json.dumps(second.model_dump()),) in host.calls
    assert result.diagnostics.prompt_tokens != result.diagnostics.current_tokens


@pytest.mark.asyncio
async def test_uncacheable_values_still_reach_the_original_counter():
    class OpaqueCounter:
        calls = 0

        async def count(self, messages):
            self.calls += 1
            return 7

    counter = OpaqueCounter()
    current = Message(role="user", content="hello", provider_specific_fields={"opaque": object()})
    result = await ContextCompactor(ContextBudget(), token_counter=counter).compact_async(
        prompt_messages=[], history_messages=[], current_messages=[current]
    )

    assert result.tokens_before == result.tokens_after == 7
    assert result.messages[-1].content == "hello"
    assert counter.calls > 0


@pytest.mark.asyncio
async def test_nested_provider_metadata_mutation_is_counted_again():
    class MutatingHost(CountingHost):
        async def count_tokens(self, *, messages, **kwargs):
            tokens = await super().count_tokens(messages=messages, **kwargs)
            if len(self.calls) == 1:
                messages[-1].provider_specific_fields["signature"]["value"] = "updated"
            return tokens

    host = MutatingHost()
    current = Message(role="user", content="same", provider_specific_fields={"signature": {"value": "old"}})
    result = await compactor(host).compact_async(prompt_messages=[], history_messages=[], current_messages=[current])

    assert result.tokens_before != result.tokens_after
    assert result.tokens_after == 7 + sum(map(len, payload(result.messages)))
    assert result.messages[-1].provider_specific_fields["signature"]["value"] == "updated"


@pytest.mark.asyncio
async def test_message_order_is_part_of_the_counted_payload():
    host = CountingHost()
    messages = [Message(role="user", content="first"), Message(role="user", content="second")]
    result = await compactor(host).compact_async(
        prompt_messages=messages, history_messages=[], current_messages=list(reversed(messages))
    )

    assert payload(result.frame.prompt) in host.calls
    assert payload(result.frame.current) in host.calls
    assert len(host.calls) == 4
