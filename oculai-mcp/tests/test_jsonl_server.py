import asyncio

import pytest

from oculai_mcp.jsonl_server import JsonlRequestServer, _read_request_lines


@pytest.mark.unit
async def test_requests_execute_concurrently_and_complete_out_of_order():
    started: list[str] = []
    releases = {"slow": asyncio.Event(), "fast": asyncio.Event()}
    responses: list[dict] = []

    async def handler(params):
        name = params["name"]
        started.append(name)
        await releases[name].wait()
        return {"name": name}

    server = JsonlRequestServer(lambda _method: handler, _writer(responses))
    await server.accept({"id": "slow", "method": "tool", "params": {"name": "slow"}})
    await server.accept({"id": "fast", "method": "tool", "params": {"name": "fast"}})
    await _eventually(lambda: len(started) == 2)

    releases["fast"].set()
    await _eventually(lambda: len(responses) == 1)
    releases["slow"].set()
    await server.drain()

    assert started == ["slow", "fast"]
    assert [response["id"] for response in responses] == ["fast", "slow"]


@pytest.mark.unit
async def test_cancel_frame_cancels_target_and_settles_both_requests():
    entered = asyncio.Event()
    responses: list[dict] = []

    async def handler(_params):
        entered.set()
        await asyncio.Event().wait()

    server = JsonlRequestServer(lambda _method: handler, _writer(responses))
    await server.accept({"id": "work", "method": "tool", "params": {}})
    await entered.wait()
    await server.accept({"id": "cancel", "method": "$cancel", "params": {"request_id": "work"}})
    await _eventually(lambda: len(responses) == 2)

    by_id = {response["id"]: response for response in responses}
    assert by_id["cancel"]["result"] == {"request_id": "work", "cancelled": True}
    assert by_id["work"]["error"]["code"] == "CANCELLED"


@pytest.mark.unit
async def test_failure_is_isolated_from_other_in_flight_request():
    responses: list[dict] = []

    async def handler(params):
        if params["fail"]:
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        return {"value": 42}

    server = JsonlRequestServer(lambda _method: handler, _writer(responses))
    await server.accept({"id": "bad", "method": "tool", "params": {"fail": True}})
    await server.accept({"id": "good", "method": "tool", "params": {"fail": False}})
    await server.drain()

    by_id = {response["id"]: response for response in responses}
    assert by_id["bad"]["error"]["code"] == "TOOL_ERROR"
    assert by_id["good"] == {"id": "good", "ok": True, "result": {"value": 42}}


@pytest.mark.unit
async def test_eof_drain_cancels_requests_after_timeout():
    entered = asyncio.Event()
    responses: list[dict] = []

    async def handler(_params):
        entered.set()
        await asyncio.Event().wait()

    server = JsonlRequestServer(lambda _method: handler, _writer(responses), drain_timeout=0.01)
    await server.accept({"id": "work", "method": "tool", "params": {}})
    await entered.wait()
    await server.drain()

    assert responses[0]["id"] == "work"
    assert responses[0]["error"]["code"] == "CANCELLED"
    assert server.in_flight == 0


@pytest.mark.unit
async def test_thread_compatible_line_reader_dispatches_until_eof():
    responses: list[dict] = []
    lines = iter([
        b'{"id":"one","method":"tool","params":{"value":7}}\n',
        b"",
    ])

    async def readline() -> bytes:
        return next(lines)

    async def handler(params):
        return {"echo": params["value"]}

    server = JsonlRequestServer(lambda _method: handler, _writer(responses))
    await _read_request_lines(readline, server)
    await server.drain()

    assert responses == [{"id": "one", "ok": True, "result": {"echo": 7}}]


def _writer(responses: list[dict]):
    lock = asyncio.Lock()

    async def write(message: dict):
        # Mirrors the production stdout lock and deliberately yields inside it.
        async with lock:
            await asyncio.sleep(0)
            responses.append(message)

    return write


async def _eventually(predicate, attempts: int = 100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")
