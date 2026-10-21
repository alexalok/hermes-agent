"""Scheduler-only ownership seams for the optional worker presentation channel."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
from pathlib import Path

from cron.approval_relay import presenters_for
from cron.approval_relay_wire import supported
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_dispatch = contextvars.ContextVar("cron_approval_dispatch", default=None)
_worker = contextvars.ContextVar("cron_approval_worker", default=None)
_relays = set()
_lock = threading.RLock()
_gateway_active = False
_gateway_generation = 0


@contextlib.contextmanager
def dispatch_scope(job):
    """Never recover authority from disk; only this live spawn may attach a peer."""
    relay = None
    home = str(get_hermes_home().resolve())
    with _lock:
        active, generation = _gateway_active, _gateway_generation
    if active and supported() and presenters_for(home):
        from cron.approval_relay_server import WorkerApprovalRelay

        try:
            relay = WorkerApprovalRelay(home, str(job["id"]), str(job["execution_id"]))
            with _lock:
                if _gateway_active and generation == _gateway_generation:
                    _relays.add(relay)
                else:
                    relay.close()
                    relay = None
        except Exception:
            logger.warning("Cron approval channel unavailable; retaining external approval wait", exc_info=True)
    token = _dispatch.set(relay)
    try:
        yield
    finally:
        _dispatch.reset(token)
        if relay is not None:
            relay.close()
            with _lock:
                _relays.discard(relay)


def dispatch_descriptor():
    relay = _dispatch.get()
    return relay.descriptor if relay is not None else None


def attach_spawned_worker(process) -> None:
    relay = _dispatch.get()
    if relay is not None:
        try:
            relay.attach(process.pid)
        except Exception:
            relay.close()
            logger.warning("Cron approval worker identity unavailable", exc_info=True)


def close_gateway_relays() -> None:
    """Invalidate presentations at gateway shutdown without signalling workers."""
    global _gateway_active, _gateway_generation
    with _lock:
        _gateway_active = False
        _gateway_generation += 1
        for relay in _relays:
            relay.close()


def start_gateway_relays() -> None:
    """Only an explicit new gateway lifecycle can accept new worker channels."""
    global _gateway_active
    with _lock:
        close_gateway_relays()
        _gateway_active = True


@contextlib.contextmanager
def worker_scope(payload):
    client = None
    descriptor = payload.get("approval_relay")
    if descriptor is not None and supported():
        from cron.approval_relay_client import WorkerClient

        try:
            job = payload["job"]
            if (descriptor["profile_home"] != str(Path(payload["profile_home"]).resolve())
                    or descriptor["job_id"] != str(job["id"])
                    or descriptor["execution_id"] != str(job["execution_id"])):
                raise ValueError("Cron approval dispatch mismatch")
            client = WorkerClient(descriptor)
        except Exception:
            logger.warning("Cron approval dispatch unavailable", exc_info=True)
    token = _worker.set(client)
    try:
        yield
    finally:
        if client is not None:
            client.close()
        _worker.reset(token)


def bind_worker_agent(agent) -> None:
    from cron.execution_identity import current_cron_execution

    client = _worker.get()
    execution = current_cron_execution()
    if (client is not None and execution is not None
            and execution.job_id == client.descriptor["job_id"]
            and execution.execution_id == client.descriptor["execution_id"]
            and str(get_hermes_home().resolve()) == client.descriptor["profile_home"]):
        try:
            client.bind_agent(agent)
        except Exception:
            client.close()
            logger.warning("Cron approval session unavailable; retaining external approval wait", exc_info=True)


def current_worker():
    return _worker.get()
