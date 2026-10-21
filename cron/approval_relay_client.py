"""Worker-side presentation context. No decisions or tool dispatch cross this API."""

from __future__ import annotations

import contextlib
import logging
import math
import os
import socket
import threading
import time
import uuid

from agent.memory_provider import spawn_context_thread
from cron.approval_relay import ApprovalUnavailable
from cron.approval_relay_wire import HEARTBEAT_SECONDS, IO_TIMEOUT, Peer, receive_frame, send_frame

logger = logging.getLogger(__name__)


class WorkerClient:
    def __init__(self, descriptor: dict):
        self.descriptor = dict(descriptor)
        self.peer = Peer(**descriptor["gateway"])
        self._pid = os.getpid()
        self._session_id = None
        self._agent = None
        self._session_lock = threading.RLock()
        self._unavailable = threading.Event()

    @contextlib.contextmanager
    def _connect(self):
        if self._pid != os.getpid() or self._unavailable.is_set():
            raise ApprovalUnavailable("Cron approval channel unavailable")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(IO_TIMEOUT)
            connection.connect(self.descriptor["address"])
            self.peer.verify(connection)
        except (OSError, ValueError):
            self._unavailable.set()
            connection.close()
            raise
        try:
            yield connection
        finally:
            connection.close()

    def bind_session(self, session_id: str) -> None:
        with self._session_lock, self._connect() as connection:
            send_frame(connection, {"op": "session", "session_id": session_id,
                                    "previous_session_id": self._session_id})
            if receive_frame(connection, self.peer) != {"ok": True}:
                raise ApprovalUnavailable("Cron session binding refused")
            self._session_id = session_id

    def bind_agent(self, agent) -> None:
        self.bind_session(agent.session_id)
        self._agent = agent

    def _sync_session(self, session_id):
        with self._session_lock:
            actual = self._agent.session_id if self._agent is not None else self._session_id
            if not actual or session_id != actual:
                raise ApprovalUnavailable("Cron approval session mismatch")
            if actual != self._session_id:
                self.bind_session(actual)

    @contextlib.contextmanager
    def pending(self, provider: str, *, session_id: str, external_session_id: str,
                call_id: str, deadline: float, check):
        check()
        self._sync_session(session_id)
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ApprovalUnavailable("Cron approval deadline expired")
        with self._connect() as connection:
            send_frame(connection, {
                "op": "open", "provider": provider, "session_id": session_id,
                "external_session_id": external_session_id, "call_id": call_id,
                "invocation_id": uuid.uuid4().hex,
                "deadline": min(deadline, time.monotonic() + 600),
            })
            if receive_frame(connection, self.peer) != {"ok": True}:
                raise ApprovalUnavailable("Cron approval presentation refused")
            stopped = threading.Event()
            send_lock = threading.Lock()

            def pulse():
                try:
                    while not stopped.wait(HEARTBEAT_SECONDS):
                        if self._unavailable.is_set():
                            raise ApprovalUnavailable("Cron approval channel closed")
                        check()
                        if time.monotonic() >= deadline:
                            raise ApprovalUnavailable("expired")
                        with send_lock:
                            if not stopped.is_set():
                                send_frame(connection, {"op": "heartbeat"})
                except Exception:
                    logger.debug("Cron approval heartbeat stopped", exc_info=True)
                    stopped.set()
                    # A deadline/cancellation or rejected call retires only this
                    # invocation. Other calls on the supervised worker stay live.
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)

            spawn_context_thread(pulse, name="cron-approval-heartbeat").start()
            reason = "completed"
            try:
                check()
                yield
            except BaseException:
                reason = "expired" if time.monotonic() >= deadline else "cancelled"
                raise
            finally:
                stopped.set()
                with send_lock, contextlib.suppress(OSError):
                    send_frame(connection, {"op": "close", "reason": reason})

    def close(self) -> None:
        self._unavailable.set()
