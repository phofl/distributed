from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from distributed import Worker, wait
from distributed.protocol.serialize import Serialize
from distributed.utils import recursive_to_dict
from distributed.utils_test import (
    BlockedGetData,
    _LockedCommPool,
    assert_story,
    freeze_data_fetching,
    gen_cluster,
    inc,
)
from distributed.worker_state_machine import (
    AcquireReplicasEvent,
    ComputeTaskEvent,
    ExecuteFailureEvent,
    ExecuteSuccessEvent,
    Instruction,
    RecommendationsConflict,
    ReleaseWorkerDataMsg,
    RescheduleEvent,
    RescheduleMsg,
    SerializedTask,
    StateMachineEvent,
    TaskState,
    TaskStateState,
    UpdateDataEvent,
    merge_recs_instructions,
)


async def wait_for_state(key: str, state: TaskStateState, dask_worker: Worker) -> None:
    while key not in dask_worker.tasks or dask_worker.tasks[key].state != state:
        await asyncio.sleep(0.005)


def test_TaskState_get_nbytes():
    assert TaskState("x", nbytes=123).get_nbytes() == 123
    # Default to distributed.scheduler.default-data-size
    assert TaskState("y").get_nbytes() == 1024


def test_TaskState__to_dict():
    """Tasks that are listed as dependencies or dependents of other tasks are dumped as
    a short repr and always appear in full directly under Worker.tasks. Uninteresting
    fields are omitted.
    """
    x = TaskState("x", state="memory", done=True)
    y = TaskState("y", priority=(0,), dependencies={x})
    x.dependents.add(y)
    actual = recursive_to_dict([x, y])
    assert actual == [
        {
            "key": "x",
            "state": "memory",
            "done": True,
            "dependents": ["<TaskState 'y' released>"],
        },
        {
            "key": "y",
            "state": "released",
            "dependencies": ["<TaskState 'x' memory>"],
            "priority": [0],
        },
    ]


def traverse_subclasses(cls: type) -> Iterator[type]:
    yield cls
    for subcls in cls.__subclasses__():
        yield from traverse_subclasses(subcls)


@pytest.mark.parametrize(
    "cls",
    [
        *traverse_subclasses(Instruction),
        *traverse_subclasses(StateMachineEvent),
    ],
)
def test_slots(cls):
    params = [
        k
        for k in dir(cls)
        if not k.startswith("_")
        and k not in ("op", "handled")
        and not callable(getattr(cls, k))
    ]
    inst = cls(**dict.fromkeys(params))
    assert not hasattr(inst, "__dict__")


def test_sendmsg_to_dict():
    # Arbitrary sample class
    smsg = ReleaseWorkerDataMsg(key="x", stimulus_id="test")
    assert smsg.to_dict() == {
        "op": "release-worker-data",
        "key": "x",
        "stimulus_id": "test",
    }


def test_merge_recs_instructions():
    x = TaskState("x")
    y = TaskState("y")
    instr1 = RescheduleMsg(key="foo", stimulus_id="test")
    instr2 = RescheduleMsg(key="bar", stimulus_id="test")
    assert merge_recs_instructions(
        ({x: "memory"}, [instr1]),
        ({y: "released"}, [instr2]),
    ) == (
        {x: "memory", y: "released"},
        [instr1, instr2],
    )

    # Identical recommendations are silently ignored; incompatible ones raise
    assert merge_recs_instructions(({x: "memory"}, []), ({x: "memory"}, [])) == (
        {x: "memory"},
        [],
    )
    with pytest.raises(RecommendationsConflict):
        merge_recs_instructions(({x: "memory"}, []), ({x: "released"}, []))


def test_event_to_dict():
    ev = RescheduleEvent(stimulus_id="test", key="x")
    ev2 = ev.to_loggable(handled=11.22)
    assert ev2 == ev
    d = recursive_to_dict(ev2)
    assert d == {
        "cls": "RescheduleEvent",
        "stimulus_id": "test",
        "handled": 11.22,
        "key": "x",
    }
    ev3 = StateMachineEvent.from_dict(d)
    assert ev3 == ev


def test_computetask_to_dict():
    """The potentially very large ComputeTaskEvent.run_spec is not stored in the log"""
    ev = ComputeTaskEvent(
        key="x",
        who_has={"y": ["w1"]},
        nbytes={"y": 123},
        priority=(0,),
        duration=123.45,
        # Automatically converted to SerializedTask on init
        run_spec={"function": b"blob", "args": b"blob"},
        resource_restrictions={},
        actor=False,
        annotations={},
        stimulus_id="test",
    )
    assert ev.run_spec == SerializedTask(function=b"blob", args=b"blob")
    ev2 = ev.to_loggable(handled=11.22)
    assert ev2.handled == 11.22
    assert ev2.run_spec == SerializedTask(task=None)
    assert ev.run_spec == SerializedTask(function=b"blob", args=b"blob")
    d = recursive_to_dict(ev2)
    assert d == {
        "cls": "ComputeTaskEvent",
        "key": "x",
        "who_has": {"y": ["w1"]},
        "nbytes": {"y": 123},
        "priority": [0],
        "run_spec": [None, None, None, None],
        "duration": 123.45,
        "resource_restrictions": {},
        "actor": False,
        "annotations": {},
        "stimulus_id": "test",
        "handled": 11.22,
    }
    ev3 = StateMachineEvent.from_dict(d)
    assert isinstance(ev3, ComputeTaskEvent)
    assert ev3.run_spec == SerializedTask(task=None)
    assert ev3.priority == (0,)  # List is automatically converted back to tuple


def test_updatedata_to_dict():
    """The potentially very large UpdateDataEvent.data is not stored in the log"""
    ev = UpdateDataEvent(
        data={"x": "foo", "y": "bar"},
        report=True,
        stimulus_id="test",
    )
    ev2 = ev.to_loggable(handled=11.22)
    assert ev2.handled == 11.22
    assert ev2.data == {"x": None, "y": None}
    d = recursive_to_dict(ev2)
    assert d == {
        "cls": "UpdateDataEvent",
        "data": {"x": None, "y": None},
        "report": True,
        "stimulus_id": "test",
        "handled": 11.22,
    }
    ev3 = StateMachineEvent.from_dict(d)
    assert isinstance(ev3, UpdateDataEvent)
    assert ev3.data == {"x": None, "y": None}


def test_executesuccess_to_dict():
    """The potentially very large ExecuteSuccessEvent.value is not stored in the log"""
    ev = ExecuteSuccessEvent(
        stimulus_id="test",
        key="x",
        value=123,
        start=123.4,
        stop=456.7,
        nbytes=890,
        type=int,
    )
    ev2 = ev.to_loggable(handled=11.22)
    assert ev2.value is None
    assert ev.value == 123
    d = recursive_to_dict(ev2)
    assert d == {
        "cls": "ExecuteSuccessEvent",
        "stimulus_id": "test",
        "handled": 11.22,
        "key": "x",
        "value": None,
        "nbytes": 890,
        "start": 123.4,
        "stop": 456.7,
        "type": "<class 'int'>",
    }
    ev3 = StateMachineEvent.from_dict(d)
    assert isinstance(ev3, ExecuteSuccessEvent)
    assert ev3.stimulus_id == "test"
    assert ev3.handled == 11.22
    assert ev3.key == "x"
    assert ev3.value is None
    assert ev3.start == 123.4
    assert ev3.stop == 456.7
    assert ev3.nbytes == 890
    assert ev3.type is None


def test_executefailure_to_dict():
    ev = ExecuteFailureEvent(
        stimulus_id="test",
        key="x",
        start=123.4,
        stop=456.7,
        exception=Serialize(ValueError("foo")),
        traceback=Serialize("lose me"),
        exception_text="exc text",
        traceback_text="tb text",
    )
    ev2 = ev.to_loggable(handled=11.22)
    assert ev2 == ev
    d = recursive_to_dict(ev2)
    assert d == {
        "cls": "ExecuteFailureEvent",
        "stimulus_id": "test",
        "handled": 11.22,
        "key": "x",
        "start": 123.4,
        "stop": 456.7,
        "exception": "<Serialize: foo>",
        "traceback": "<Serialize: lose me>",
        "exception_text": "exc text",
        "traceback_text": "tb text",
    }
    ev3 = StateMachineEvent.from_dict(d)
    assert isinstance(ev3, ExecuteFailureEvent)
    assert ev3.stimulus_id == "test"
    assert ev3.handled == 11.22
    assert ev3.key == "x"
    assert ev3.start == 123.4
    assert ev3.stop == 456.7
    assert isinstance(ev3.exception, Serialize)
    assert isinstance(ev3.exception.data, Exception)
    assert ev3.traceback is None
    assert ev3.exception_text == "exc text"
    assert ev3.traceback_text == "tb text"


@gen_cluster(client=True)
async def test_fetch_to_compute(c, s, a, b):
    with freeze_data_fetching(b):
        f1 = c.submit(inc, 1, workers=[a.address], key="f1", allow_other_workers=True)
        f2 = c.submit(inc, f1, workers=[b.address], key="f2")
        await wait_for_state(f1.key, "fetch", b)
        await a.close()

    await f2

    assert_story(
        b.log,
        # FIXME: This log should be replaced with a StateMachineEvent log
        [
            (f2.key, "compute-task", "released"),
            # This is a "please fetch" request. We don't have anything like
            # this, yet. We don't see the request-dep signal in here because we
            # do not wait for the key to be actually scheduled
            (f1.key, "ensure-task-exists", "released"),
            # After the worker failed, we're instructed to forget f2 before
            # something new comes in
            ("free-keys", (f2.key,)),
            (f1.key, "compute-task", "released"),
            (f1.key, "put-in-memory"),
            (f2.key, "compute-task", "released"),
        ],
    )


@gen_cluster(client=True)
async def test_fetch_via_amm_to_compute(c, s, a, b):
    with freeze_data_fetching(b):
        f1 = c.submit(inc, 1, workers=[a.address], key="f1", allow_other_workers=True)
        await f1
        s.request_acquire_replicas(b.address, [f1.key], stimulus_id="test")
        await wait_for_state(f1.key, "fetch", b)
        await a.close()

    await f1

    assert_story(
        b.log,
        # FIXME: This log should be replaced with a StateMachineEvent log
        [
            (f1.key, "ensure-task-exists", "released"),
            (f1.key, "released", "fetch", "fetch", {}),
            (f1.key, "compute-task", "fetch"),
            (f1.key, "put-in-memory"),
        ],
    )


@pytest.mark.parametrize("as_deps", [False, True])
@gen_cluster(client=True, nthreads=[("", 1)] * 3)
async def test_lose_replica_during_fetch(c, s, w1, w2, w3, as_deps):
    """
    as_deps=True
        0. task x is a dependency of y1 and y2
        1. scheduler calls handle_compute("y1", who_has={"x": [w2, w3]}) on w1
        2. x transitions released -> fetch
        3. the network stack is busy, so x does not transition to flight yet.
        4. scheduler calls handle_compute("y2", who_has={"x": [w3]}) on w1
        5. when x finally reaches the top of the data_needed heap, w1 will not try
           contacting w2

    as_deps=False
        1. scheduler calls handle_acquire_replicas(who_has={"x": [w2, w3]}) on w1
        2. x transitions released -> fetch
        3. the network stack is busy, so x does not transition to flight yet.
        4. scheduler calls handle_acquire_replicas(who_has={"x": [w3]}) on w1
        5. when x finally reaches the top of the data_needed heap, w1 will not try
           contacting w2
    """
    x = (await c.scatter({"x": 1}, workers=[w2.address, w3.address], broadcast=True))[
        "x"
    ]

    # Make sure find_missing is not involved
    w1.periodic_callbacks["find-missing"].stop()

    with freeze_data_fetching(w1, jump_start=True):
        if as_deps:
            y1 = c.submit(inc, x, key="y1", workers=[w1.address])
        else:
            s.request_acquire_replicas(w1.address, ["x"], stimulus_id="test")

        await wait_for_state("x", "fetch", w1)
        assert w1.tasks["x"].who_has == {w2.address, w3.address}

        assert len(s.tasks["x"].who_has) == 2
        await w2.close()
        while len(s.tasks["x"].who_has) > 1:
            await asyncio.sleep(0.01)

        if as_deps:
            y2 = c.submit(inc, x, key="y2", workers=[w1.address])
        else:
            s.request_acquire_replicas(w1.address, ["x"], stimulus_id="test")

        while w1.tasks["x"].who_has != {w3.address}:
            await asyncio.sleep(0.01)

    await wait_for_state("x", "memory", w1)
    assert_story(
        w1.story("request-dep"),
        [("request-dep", w3.address, {"x"})],
        # This tests that there has been no attempt to contact w2.
        # If the assumption being tested breaks, this will fail 50% of the times.
        strict=True,
    )


@gen_cluster(client=True, nthreads=[("", 1)] * 2)
async def test_fetch_to_missing(c, s, a, b):
    """
    1. task x is a dependency of y
    2. scheduler calls handle_compute("y", who_has={"x": [b]}) on a
    3. x transitions released -> fetch -> flight; a connects to b
    4. b responds it's busy. x transitions flight -> fetch
    5. The busy state triggers an RPC call to Scheduler.who_has
    6. the scheduler responds {"x": []}, because w1 in the meantime has lost the key.
    7. x is transitioned fetch -> missing
    """
    x = await c.scatter({"x": 1}, workers=[b.address])
    b.total_in_connections = 0
    # Crucially, unlike with `c.submit(inc, x, workers=[a.address])`, the scheduler
    # doesn't keep track of acquire-replicas requests, so it won't proactively inform a
    # when we call remove_worker later on
    s.request_acquire_replicas(a.address, ["x"], stimulus_id="test")

    # state will flip-flop between fetch and flight every 150ms, which is the retry
    # period for busy workers.
    await wait_for_state("x", "fetch", a)
    assert b.address in a.busy_workers

    # Sever connection between b and s, but not between b and a.
    # If a tries fetching from b after this, b will keep responding {status: busy}.
    b.periodic_callbacks["heartbeat"].stop()
    await s.remove_worker(b.address, close=False, stimulus_id="test")

    await wait_for_state("x", "missing", a)

    assert_story(
        a.story("x"),
        [
            ("x", "ensure-task-exists", "released"),
            ("x", "released", "fetch", "fetch", {}),
            ("gather-dependencies", b.address, {"x"}),
            ("x", "fetch", "flight", "flight", {}),
            ("request-dep", b.address, {"x"}),
            ("busy-gather", b.address, {"x"}),
            ("x", "flight", "fetch", "fetch", {}),
            ("x", "fetch", "missing", "missing", {}),
        ],
        # There may be a round of find_missing() after this.
        # Due to timings, there also may be multiple attempts to connect from a to b.
        strict=False,
    )


@pytest.mark.skip(reason="https://github.com/dask/distributed/issues/6446")
@gen_cluster(client=True)
async def test_new_replica_while_all_workers_in_flight(c, s, w1, w2):
    """A task is stuck in 'fetch' state because all workers that hold a replica are in
    flight. While in this state, a new replica appears on a different worker and the
    scheduler informs the waiting worker through a new acquire-replicas or
    compute-task op.

    In real life, this will typically happen when the Active Memory Manager replicates a
    key to multiple workers and some workers are much faster than others to acquire it,
    due to unrelated tasks being in flight, so 2 seconds later the AMM reiterates the
    request, passing a larger who_has.

    Test that, when this happens, the task is immediately acquired from the new worker,
    without waiting for the original replica holders to get out of flight.
    """
    # Make sure find_missing is not involved
    w1.periodic_callbacks["find-missing"].stop()

    async with BlockedGetData(s.address) as w3:
        x = c.submit(inc, 1, key="x", workers=[w3.address])
        y = c.submit(inc, 2, key="y", workers=[w3.address])
        await wait([x, y])
        s.request_acquire_replicas(w1.address, ["x"], stimulus_id="test")
        await w3.in_get_data.wait()
        assert w1.tasks["x"].state == "flight"
        s.request_acquire_replicas(w1.address, ["y"], stimulus_id="test")
        # This cannot progress beyond fetch because w3 is already in flight
        await wait_for_state("y", "fetch", w1)

        # Simulate that the AMM also requires that w2 acquires a replica of x.
        # The replica lands on w2 soon afterwards, while w3->w1 comms remain blocked by
        # unrelated transfers (x in our case).
        w2.update_data({"y": 3}, report=True)
        ws2 = s.workers[w2.address]
        while ws2 not in s.tasks["y"].who_has:
            await asyncio.sleep(0.01)

        # 2 seconds later, the AMM reiterates that w1 should acquire a replica of y
        s.request_acquire_replicas(w1.address, ["y"], stimulus_id="test")
        await wait_for_state("y", "memory", w1)

        # Finally let the other worker to get out of flight
        w3.block_get_data.set()
        await wait_for_state("x", "memory", w1)


@gen_cluster(client=True)
async def test_cancelled_while_in_flight(c, s, a, b):
    event = asyncio.Event()
    a.rpc = _LockedCommPool(a.rpc, write_event=event)

    x = c.submit(inc, 1, key="x", workers=[b.address])
    y = c.submit(inc, x, key="y", workers=[a.address])
    await wait_for_state("x", "flight", a)
    y.release()
    await wait_for_state("x", "cancelled", a)

    # Let the comm from b to a return the result
    event.set()
    # upon reception, x transitions cancelled->forgotten
    while a.tasks:
        await asyncio.sleep(0.01)


@gen_cluster(client=True)
async def test_in_memory_while_in_flight(c, s, a, b):
    """
    1. A client scatters x to a
    2. The scheduler does not know about scattered keys until the three-way round-trip
       between client, worker, and scheduler has been completed (see Scheduler.scatter)
    3. In the middle of that handshake, a client (not necessarily the same client) calls
       ``{op: compute-task, key: x}`` on b and then
       ``{op: compute-task, key: y, who_has: {x: [b]}`` on a, which triggers a
       gather_dep call to copy x key from b to a.
    4. while x is in flight from b to a, the scatter finishes, which triggers
       update_data, which in turn transitions x from flight to memory.
    5. later on, gather_dep finishes, but the key is already in memory.
    """
    event = asyncio.Event()
    a.rpc = _LockedCommPool(a.rpc, write_event=event)

    x = c.submit(inc, 1, key="x", workers=[b.address])
    y = c.submit(inc, x, key="y", workers=[a.address])
    await wait_for_state("x", "flight", a)
    a.update_data({"x": 3})
    await wait_for_state("x", "memory", a)

    # Let the comm from b to a return the result
    event.set()
    assert await y == 4  # Data in flight from b has been discarded


@gen_cluster(client=True)
async def test_forget_data_needed(c, s, a, b):
    """
    1. A task transitions to fetch and is added to data_needed
    2. _ensure_communicating runs, but the network is saturated so the task is not
       popped from data_needed
    3. Task is forgotten
    4. Task is recreated from scratch and transitioned to fetch again
    5. BUG: at the moment of writing this test, adding to data_needed silently did
       nothing, because it still contained the forgotten task, which is a different
       TaskState instance which will be no longer updated.
    6. _ensure_communicating runs. It pops the forgotten task and discards it.
    7. We now have a task stuck in fetch state.
    """
    x = c.submit(inc, 1, key="x", workers=[a.address])
    with freeze_data_fetching(b):
        y = c.submit(inc, x, key="y", workers=[b.address])
        await wait_for_state("x", "fetch", b)
        x.release()
        y.release()
        while s.tasks or a.tasks or b.tasks:
            await asyncio.sleep(0.01)

    x = c.submit(inc, 2, key="x", workers=[a.address])
    y = c.submit(inc, x, key="y", workers=[b.address])
    assert await y == 4


@gen_cluster(client=True, nthreads=[("", 1)] * 3)
async def test_missing_handle_compute_dependency(c, s, w1, w2, w3):
    """Test that it is OK for a dependency to be in state missing if a dependent is
    asked to be computed
    """
    w3.periodic_callbacks["find-missing"].stop()

    f1 = c.submit(inc, 1, key="f1", workers=[w1.address])
    f2 = c.submit(inc, 2, key="f2", workers=[w1.address])
    await wait_for_state(f1.key, "memory", w1)

    w3.handle_stimulus(
        AcquireReplicasEvent(who_has={f1.key: [w2.address]}, stimulus_id="acquire")
    )
    await wait_for_state(f1.key, "missing", w3)

    f3 = c.submit(sum, [f1, f2], key="f3", workers=[w3.address])

    await f3


@gen_cluster(client=True, nthreads=[("", 1)] * 3)
async def test_missing_to_waiting(c, s, w1, w2, w3):
    w3.periodic_callbacks["find-missing"].stop()

    f1 = c.submit(inc, 1, key="f1", workers=[w1.address], allow_other_workers=True)
    await wait_for_state(f1.key, "memory", w1)

    w3.handle_stimulus(
        AcquireReplicasEvent(who_has={f1.key: [w2.address]}, stimulus_id="acquire")
    )
    await wait_for_state(f1.key, "missing", w3)

    await w2.close()
    await w1.close()

    await f1