import asyncio
import json
import os
import sys


async def main():
    print(json.dumps({"type": "ready", "tools": 3, "pid": os.getpid()}), file=sys.stderr, flush=True)
    tasks = {}
    write_lock = asyncio.Lock()

    async def emit(message):
        async with write_lock:
            print(json.dumps(message), flush=True)

    async def execute(request):
        request_id = request["id"]
        method = request["method"]
        params = request.get("params", {})
        try:
            if method == "echo":
                await asyncio.sleep(float(params.get("delay", 0)))
                await emit({"id": request_id, "ok": True, "result": params})
            elif method == "hang":
                await asyncio.Event().wait()
            elif method == "crash":
                os._exit(23)
            else:
                await emit({"id": request_id, "ok": False, "error": {"code": "UNKNOWN", "message": method}})
        except asyncio.CancelledError:
            await emit({"id": request_id, "ok": False, "error": {"code": "CANCELLED", "message": "cancelled"}})
            raise

    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        request = json.loads(line)
        if request["method"] == "$cancel":
            target_id = request.get("params", {}).get("request_id")
            target = tasks.get(target_id)
            if target:
                target.cancel()
            await emit({"id": request["id"], "ok": True, "result": {"cancelled": bool(target)}})
            continue
        task = asyncio.create_task(execute(request))
        tasks[request["id"]] = task
        task.add_done_callback(lambda _task, key=request["id"]: tasks.pop(key, None))

    if tasks:
        await asyncio.gather(*tasks.values(), return_exceptions=True)


asyncio.run(main())
