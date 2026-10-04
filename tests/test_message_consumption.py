from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tickneko.api import create_app
from tickneko.workflow import NodeExecutionContext, SimpleWorkflowRunner, WorkflowGraph, validate_graph
from tickneko.workflow.runtime import MessageRouter
from tickneko.workflow.models import WorkflowNode
from tickneko.workflow.nodes.consume import exec_consume


def message(**overrides):
    return {"platform": "onebot", "self_id": "10", "message_id": "42",
            "user_id": "20", "chat": "private", "chat_id": "20", "message": "awa", **overrides}


def graph():
    return {"nodes": [
        {"id": "s", "type": "start", "config": {"trigger": "message"}},
        {"id": "c", "type": "condition", "config": {"operator": "==", "right": "awa"}},
        {"id": "take", "type": "consume", "config": {}},
        {"id": "end", "type": "end", "config": {}},
    ], "edges": [
        {"source": "s", "target": "c", "source_port": "trigger", "target_port": "trigger"},
        {"source": "s", "target": "c", "source_port": "message", "target_port": "left"},
        {"source": "c", "target": "take", "source_port": "true", "target_port": "trigger"},
        {"source": "take", "target": "end", "source_port": "trigger", "target_port": "trigger"},
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, 0, "false", "", None])
async def test_unsent_message_is_not_claimed(accepted):
    ctx = NodeExecutionContext()
    ctx.trigger_data = message()
    ctx.inputs["only_if"] = accepted
    await exec_consume(WorkflowNode(id="c", type="consume"), ctx)
    assert ctx.message_consumed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("text,consumed", [("awa", True), ("hello", False)])
async def test_condition_only_claims_matching_branch(text, consumed):
    data = graph()
    assert validate_graph(data).ok
    ctx = NodeExecutionContext()
    ctx.trigger_data = message(message=text)
    notifications = []
    ctx.on_message_consumed = lambda: notifications.append(True)
    await SimpleWorkflowRunner().run(WorkflowGraph.model_validate(data), ctx)
    assert ctx.message_consumed is consumed
    assert notifications == ([True] if consumed else [])


@pytest.mark.asyncio
async def test_same_message_ws_http_and_retry_execute_once():
    router = MessageRouter()
    calls = []
    async def run(wid, version, **kw):
        calls.append(wid)
        await asyncio.sleep(0)
        kw["on_message_consumed"]()
        return True
    router.attach(run)
    router.register("flow", 1, "owner")
    assert await asyncio.gather(*(router.dispatch_once("owner", trigger_data=message()) for _ in range(5))) == [True] * 5
    assert await router.dispatch_once("owner", trigger_data=message()) is True
    assert calls == ["flow"]


@pytest.mark.asyncio
async def test_false_and_old_workflows_do_not_consume():
    router = MessageRouter()
    async def run(*args, **kwargs):
        return None
    router.attach(run)
    router.register("old", 1, "owner")
    assert await router.dispatch_once("owner", trigger_data=message()) is False
    assert await router.dispatch_once("other", trigger_data=message()) is False


@pytest.mark.asyncio
async def test_receipt_identity_isolates_bots_chats_and_owners():
    router = MessageRouter()
    calls = []
    async def run(*args, **kwargs):
        calls.append(args)
        return False
    router.attach(run)
    for owner in ("a", "b"):
        router.register("flow", 1, owner)
    await router.dispatch_once("a", trigger_data=message())
    await router.dispatch_once("b", trigger_data=message())
    await router.dispatch_once("a", trigger_data=message(self_id="11"))
    await router.dispatch_once("a", trigger_data=message(chat="group", chat_id="30"))
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_workflow_or_retry_it():
    router = MessageRouter()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def run(*args, **kwargs):
        calls.append(1)
        started.set()
        await finish.wait()
        kwargs["on_message_consumed"]()
        return True
    router.attach(run)
    router.register("flow", 1, "owner")
    waiter = asyncio.create_task(router.dispatch_once("owner", trigger_data=message()))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish.set()
    assert await router.dispatch_once("owner", trigger_data=message()) is True
    assert calls == [1]


@pytest.mark.asyncio
async def test_claim_resolves_before_remaining_workflow_finishes():
    router = MessageRouter()
    finish = asyncio.Event()
    async def run(*args, **kwargs):
        kwargs["on_message_consumed"]()
        await finish.wait()
        return True
    router.attach(run)
    router.register("flow", 1, "owner")
    assert await asyncio.wait_for(router.dispatch_once("owner", trigger_data=message()), 1) is True
    finish.set()
    await asyncio.gather(*(receipt[1] for receipt in router._receipts.values()))


@pytest.mark.asyncio
async def test_api_uses_bot_token_and_cannot_spoof_another_bot():
    calls = []
    class Tokens:
        async def resolve(self, token):
            return SimpleNamespace(id="bot", owner_id="owner", platform="onebot") if token == "valid" else None
    def roster(*, id=None):
        assert id == "owner"
        return [SimpleNamespace(bot_id="bot", self_id=10), SimpleNamespace(bot_id="other-bot", self_id=11)]
    server = SimpleNamespace(tokens=Tokens(), roster=roster)
    async def dispatch(owner, body):
        calls.append((owner, body.message_id))
        return body.message == "awa"
    app = create_app(onebot=server, message_dispatcher=dispatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {key: val for key, val in message().items() if key != "platform"}
        assert (await client.post("/api/messages/dispatch", json=body)).status_code == 401
        headers = {"Authorization": "Bearer valid"}
        reply = await client.post("/api/messages/dispatch", headers=headers, json=body)
        assert reply.status_code == 200
        assert reply.json()["data"] == {"consumed": True}
        assert (await client.post("/api/messages/dispatch", headers=headers, json={**body, "self_id": "11"})).status_code == 409
        assert (await client.post("/api/messages/dispatch", headers=headers, json={**body, "user_id": "10"})).status_code == 422
        assert calls == [("owner", "42")]
