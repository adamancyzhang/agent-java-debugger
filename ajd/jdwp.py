"""JDWP transport layer.

Implements the wire protocol basics (see
https://docs.oracle.com/javase/8/docs/technotes/guides/jpda/jdwp-spec.html):

  * the 14-byte "JDWP-Handshake"
  * command/reply packet framing (11-byte header: length, id, flags,
    cmdset, cmd)
  * a background reader thread that routes replies to waiting callers and
    raw events (cmdset 64) to an event callback

The reader thread must never issue commands itself (a command issued from
the reader thread would deadlock: its reply would arrive on the same socket
with nobody reading it).  Callers route events to a separate consumer
thread instead.
"""

import socket
import struct
import threading

HANDSHAKE = b"JDWP-Handshake"

CMDSET_VIRTUAL_MACHINE = 1
CMDSET_REFERENCE_TYPE = 2
CMDSET_CLASS_TYPE = 3
CMDSET_ARRAY_TYPE = 4
CMDSET_INTERFACE_TYPE = 5
CMDSET_METHOD = 6
CMDSET_FIELD = 8
CMDSET_OBJECT_REFERENCE = 9
CMDSET_STRING_REFERENCE = 10
CMDSET_THREAD_REFERENCE = 11
CMDSET_THREAD_GROUP_REFERENCE = 12
CMDSET_ARRAY_REFERENCE = 13
CMDSET_CLASS_LOADER_REFERENCE = 14
CMDSET_EVENT_REQUEST = 15
CMDSET_STACK_FRAME = 16
CMDSET_CLASS_OBJECT_REFERENCE = 17
CMDSET_EVENT = 64

# Error constants from the JDWP spec (partial but covers common practice).
_ERROR_NAMES = {
    0: "none",
    10: "invalid object",
    20: "invalid class",
    21: "class not prepared",
    22: "invalid method id",
    23: "invalid location",
    24: "invalid field id",
    25: "invalid frame id",
    31: "not implemented",
    32: "not suspended",
    33: "invalid slot",
    34: "invalid type",
    35: "thread not suspended",
    40: "invalid thread",
    41: "invalid thread group",
    42: "invalid priority",
    43: "thread not suspended",
    44: "invalid object",
    50: "invalid class loader",
    51: "invalid array",
    52: "class not prepared",
    60: "invalid iterator",
    70: "invalid event kind",
    71: "invalid event request id",
    72: "invalid string",
    73: "invalid monitor",
    80: "invalid OOP",
    99: "vm dead",
    100: "out of memory",
    110: "internal error",
}


class JDWPError(Exception):
    """A JDWP command returned a non-zero error code."""

    def __init__(self, code, cmdset=None, cmd=None):
        self.code = code
        self.cmdset = cmdset
        self.cmd = cmd
        name = _ERROR_NAMES.get(code, "unknown error")
        where = f" (cmdset {cmdset}/{cmd})" if cmdset is not None else ""
        super().__init__(f"JDWP error {code} ({name}){where}")


class ConnectionLost(Exception):
    pass


class JDWPConnection:
    """One TCP connection to a debugged VM plus protocol plumbing.

    Events (cmdset 64) are handed to ``on_event(data)`` from the reader
    thread; set this attribute to a handler before/right after attach.
    The handler must be cheap or hand work off to another thread.
    """

    def __init__(self):
        self._sock = None
        self._wlock = threading.Lock()
        self._pendlock = threading.Lock()
        self._pending = {}          # id -> [threading.Event, {"data": .., "error": ..}]
        self._next_id = 1
        self._closed = False
        self._reader = None
        self.on_event = None        # fn(data) — called on reader thread
        self.on_disconnect = None   # fn() — called when the socket dies

    # ------------------------------------------------------------------ API

    def attach(self, host, port, timeout=10.0):
        """TCP connect + JDWP handshake + start the reader thread."""
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(None)
        self._sock.sendall(HANDSHAKE)
        reply = self._recv_exact(len(HANDSHAKE))
        if reply != HANDSHAKE:
            raise ConnectionLost(f"bad handshake reply from {host}:{port}: {reply!r}")
        self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                        name="jdwp-reader")
        self._reader.start()

    def close(self):
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                # shutdown() unblocks the reader thread's pending recv();
                # plain close() would wait for it and hang the process.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._pendlock:
            for ev, _slot in self._pending.values():
                ev.set()

    @property
    def alive(self):
        return not self._closed

    def command(self, cmdset, cmd, data=b"", timeout=15.0):
        """Send a command packet and wait for its reply.

        Returns the reply data bytes (error code already checked).
        Raises JDWPError on a non-zero error code, TimeoutError on timeout,
        ConnectionLost if the link dies.
        """
        if self._closed:
            raise ConnectionLost("connection closed")
        pid = self._next_id
        self._next_id += 1
        ev = threading.Event()
        slot = {"data": None, "error": 0}
        with self._pendlock:
            self._pending[pid] = (ev, slot)
        try:
            payload = struct.pack(">IIBBB", 11 + len(data), pid, 0, cmdset, cmd) + data
            with self._wlock:
                if self._closed:
                    raise ConnectionLost("connection closed")
                self._sock.sendall(payload)
        except Exception:
            with self._pendlock:
                self._pending.pop(pid, None)
            raise
        if not ev.wait(timeout):
            with self._pendlock:
                self._pending.pop(pid, None)
            raise TimeoutError(f"JDWP command {cmdset}/{cmd} timed out after {timeout}s")
        if slot["error"]:
            raise JDWPError(slot["error"], cmdset, cmd)
        return slot["data"]

    # ------------------------------------------------------------- internals

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionLost("socket closed by target VM")
            buf += chunk
        return buf

    def _reader_loop(self):
        """Read packets forever; route replies and events."""
        f = self._sock.makefile("rb")
        try:
            while not self._closed:
                hdr = f.read(11)
                if not hdr:
                    break
                if len(hdr) < 11:
                    raise ConnectionLost("truncated packet header")
                flags = hdr[8]
                if flags & 0x80:  # reply header: length, id, flags, error code
                    length, pid, _flags, errcode = struct.unpack(">IIBH", hdr)
                    data = f.read(length - 11)
                    if len(data) < length - 11:
                        raise ConnectionLost("truncated packet body")
                    with self._pendlock:
                        waiter = self._pending.get(pid)
                    if waiter is not None:
                        ev, slot = waiter
                        slot["error"] = errcode
                        slot["data"] = data
                        with self._pendlock:
                            self._pending.pop(pid, None)
                        ev.set()
                else:  # command/event packet: length, id, flags, cmdset, cmd
                    length, pid, _flags, cmdset, cmd = struct.unpack(">IIBBB", hdr)
                    data = f.read(length - 11)
                    if len(data) < length - 11:
                        raise ConnectionLost("truncated packet body")
                    if cmdset == CMDSET_EVENT and self.on_event is not None:
                        self.on_event(data)
        except (OSError, EOFError, ConnectionLost):
            pass
        finally:
            try:
                f.close()
            except OSError:
                pass
            self._closed = True
            with self._pendlock:
                for ev, _slot in self._pending.values():
                    ev.set()
            if self.on_disconnect is not None:
                self.on_disconnect()
