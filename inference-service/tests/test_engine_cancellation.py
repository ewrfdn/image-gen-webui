import asyncio
import threading

import pytest

from inference_service.config import Settings
from inference_service.engine.service import Engine, ServiceError


def test_cancelled_caller_keeps_slot_until_worker_cleanup(tmp_path):
    engine = Engine(Settings(tmp_path, "infer", "admin", "cpu", "float32"))
    started = threading.Event()
    finish = threading.Event()

    def operation():
        started.set()
        assert finish.wait(5)
        return {"done": True}

    async def scenario():
        task = asyncio.create_task(engine.execute(operation))
        assert await asyncio.to_thread(started.wait, 3)
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ServiceError) as rejected:
                await engine.execute(lambda: {})
            assert rejected.value.code == "engine_busy"
            assert rejected.value.retry_after == 1
        finally:
            finish.set()
        deadline = asyncio.get_running_loop().time() + 3
        while engine.operation_lock.locked() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert await engine.execute(lambda: {"recovered": True}) == {"recovered": True}

    asyncio.run(scenario())
