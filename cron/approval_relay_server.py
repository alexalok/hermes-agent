"""An ephemeral presentation channel for one exact scheduler-spawned process.

Neither ack files nor the writable execution ledger grant authority. The parent
pins its Popen PID before accepting a frame. A replacement gateway cannot adopt
the channel, and the channel never returns approval decisions to the worker.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import secrets
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path

from agent.memory_provider import spawn_context_thread
from cron.approval_relay import ApprovalLifetime, ApprovalRequest, ApprovalUnavailable, presenters_for
from cron.approval_relay_wire import IO_TIMEOUT, STALE_SECONDS, Peer, identifier, receive_frame, send_frame, supported

logger = logging.getLogger(__name__)
MAX_ACTIVE = 16
MAX_INVOCATIONS = 1024


class WorkerApprovalRelay:
    """Bounded gateway-side presenter host. Call attach only with the spawned PID."""

    def __init__(self, home: str, job_id: str, execution_id: str):
        if not supported():
            raise ApprovalUnavailable("Cron approval relay requires Linux peer credentials")
        self.home = str(Path(home).resolve())
        self.job_id = identifier(job_id)
        self.execution_id = identifier(execution_id)
        self._presenters = presenters_for(self.home)
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._peer = None
        self._session_id = None
        self._seen = set()
        self._connections = set()
        self._waits = set()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            # Abstract sockets leave no pathname to replace or recover after a crash.
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            address = "\0hermes-cron-approval-" + secrets.token_hex(16)
            self._listener.bind(address)
            self._listener.listen(MAX_ACTIVE)
            self._listener.settimeout(0.5)
            self.descriptor = {
                "address": address, "gateway": asdict(Peer.capture(os.getpid(), os.getuid())),  # windows-footgun: ok -- Linux-only constructor
                "profile_home": self.home, "job_id": self.job_id, "execution_id": self.execution_id,
            }
        except BaseException:
            self._listener.close()
            raise

    def attach(self, pid: int) -> None:
        with self._lock:
            if self._peer is not None or self._closed.is_set():
                raise ValueError("Cron approval worker already attached or closed")
            self._peer = Peer.capture(pid, os.getuid())  # windows-footgun: ok -- Linux-only constructor
        spawn_context_thread(self._accept, name="cron-approval-listener").start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                if not self._peer.live():
                    self.close()
                continue
            except OSError:
                return
            try:
                self._peer.verify(connection)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                connection.settimeout(IO_TIMEOUT)
                with self._lock:
                    if self._closed.is_set() or len(self._connections) >= MAX_ACTIVE:
                        raise ValueError("Cron approval connection limit")
                    self._connections.add(connection)
                spawn_context_thread(self._handle, args=(connection,), name="cron-approval-monitor").start()
            except Exception:
                logger.debug("Cron approval peer refused", exc_info=True)
                connection.close()
                with self._lock:
                    self._connections.discard(connection)

    def _request(self, frame: dict) -> tuple[ApprovalRequest, object]:
        required = {"op", "provider", "session_id", "external_session_id", "call_id", "invocation_id", "deadline"}
        if set(frame) != required or frame["op"] != "open":
            raise ValueError("Invalid cron approval request")
        strings = {key: identifier(frame[key]) for key in required - {"op", "deadline"}}
        deadline = frame["deadline"]
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not 0 < deadline - time.monotonic() <= 600):
            raise ValueError("Invalid cron approval deadline")
        with self._lock:
            presenter = self._presenters.get(strings.pop("provider"))
            if presenter is None or presenter.closed.is_set() or self._closed.is_set():
                raise ValueError("Cron approval presenter unavailable")
            if strings["session_id"] != self._session_id:
                raise ValueError("Cron approval session mismatch")
            key = (presenter.plugin_id, strings["call_id"])
            invocation_key = ("invocation", strings["invocation_id"])
            if key in self._seen or invocation_key in self._seen or len(self._seen) >= MAX_INVOCATIONS * 2:
                raise ValueError("Duplicate or excessive cron approval invocation")
            # Fence before acknowledgement or presenter I/O. Never remove this on failure.
            self._seen.update((key, invocation_key))
        return ApprovalRequest(self.home, self.job_id, self.execution_id, deadline=deadline, **strings), presenter

    def _handle(self, connection: socket.socket) -> None:
        lifetime = None
        try:
            frame = receive_frame(connection, self._peer)
            if frame.get("op") == "session":
                with self._lock:
                    session = identifier(frame.get("session_id"))
                    if (set(frame) != {"op", "session_id", "previous_session_id"}
                            or frame["previous_session_id"] != self._session_id):
                        raise ValueError("Cron session binding changed")
                    self._session_id = session
                send_frame(connection, {"ok": True})
                return
            request, presenter = self._request(frame)
            last_seen = [time.monotonic()]

            def live():
                return (not self._closed.is_set() and not presenter.closed.is_set()
                        and time.monotonic() - last_seen[0] < STALE_SECONDS and self._peer.live())

            lifetime = ApprovalLifetime(request.deadline, live)
            with self._lock:
                self._waits.add(lifetime)
            send_frame(connection, {"ok": True})
            spawn_context_thread(
                lambda: presenter.context.copy().run(self._present, presenter, request, lifetime),
                name="cron-approval-presenter",
            ).start()
            connection.settimeout(0.5)
            self._monitor(connection, lifetime, last_seen)
        except Exception:
            logger.debug("Cron approval presentation ended or refused", exc_info=True)
        finally:
            if lifetime is not None:
                lifetime.close("disconnected")
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._waits.discard(lifetime)

    def _monitor(self, connection, lifetime, last_seen):
        while True:
            lifetime.check()
            try:
                frame = receive_frame(connection, self._peer)
            except TimeoutError:
                continue
            if frame == {"op": "heartbeat"}:
                last_seen[0] = time.monotonic()
            elif set(frame) == {"op", "reason"} and frame["op"] == "close" and frame["reason"] in (
                "completed", "cancelled", "expired", "failed",
            ):
                lifetime.close(frame["reason"])
                return
            else:
                raise ValueError("Invalid cron approval lifecycle frame")

    @staticmethod
    def _present(presenter, request, lifetime):
        try:
            lifetime.check()
            with presenter.callback(request, lifetime):
                lifetime.ended.wait(max(0.0, request.deadline - time.monotonic()))
                lifetime.check()
        except ApprovalUnavailable:
            pass
        except Exception:
            logger.warning("Cron approval presenter failed", exc_info=True)
        finally:
            lifetime.close("failed")

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            self._listener.close()
            for lifetime in self._waits:
                lifetime.close("disconnected")
            for connection in self._connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
