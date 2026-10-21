"""Linux-only, bounded frames authenticated on every message, including after fork."""

from __future__ import annotations

import array
import json
import os
import socket
import struct
from dataclasses import dataclass

MAX_FRAME = 8192
IO_TIMEOUT = 2.0
HEARTBEAT_SECONDS = 0.5
STALE_SECONDS = 3.0


def supported() -> bool:
    from hermes_platform.host.facts import os_family

    return os_family() == "linux" and all(hasattr(socket, name) for name in
                                 ("SO_PEERCRED", "SO_PASSCRED", "SCM_CREDENTIALS", "SOCK_SEQPACKET"))


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    started_at: int

    @classmethod
    def capture(cls, pid: int, uid: int) -> Peer:
        from gateway.status import get_process_start_time

        start = get_process_start_time(pid)
        if type(pid) is not int or pid <= 1 or start is None:
            raise ValueError("Cannot establish cron approval process identity")
        return cls(pid, uid, start)

    def live(self) -> bool:
        from gateway.status import get_process_start_time

        return get_process_start_time(self.pid) == self.started_at

    def verify(self, connection: socket.socket) -> None:
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if (pid, uid) != (self.pid, self.uid) or not self.live():
            raise PermissionError("Unsupervised cron approval peer")


def send_frame(connection: socket.socket, frame: dict) -> None:
    wire = json.dumps(frame, allow_nan=False, separators=(",", ":")).encode()
    if len(wire) > MAX_FRAME or connection.send(wire) != len(wire):
        raise ValueError("Invalid cron approval frame size")


def _invalid_constant(value):
    raise ValueError("Non-finite cron approval value")


def receive_frame(connection: socket.socket, peer: Peer) -> dict:
    data, ancillary, flags, _ = connection.recvmsg(MAX_FRAME, socket.CMSG_SPACE(256), socket.MSG_CMSG_CLOEXEC)
    credentials = []
    unexpected = False
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) == 12:
            credentials.append(struct.unpack("3i", value)[:2])
        else:
            unexpected = True
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(value[:len(value) - len(value) % fds.itemsize])
                for fd in fds:
                    os.close(fd)
    if (not data or unexpected or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or credentials != [(peer.pid, peer.uid)] or not peer.live()):
        raise PermissionError("Invalid cron approval frame or process identity")
    result = json.loads(data, parse_constant=_invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("Cron approval frame must be an object")
    return result


def identifier(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid cron approval identifier")
    return value
