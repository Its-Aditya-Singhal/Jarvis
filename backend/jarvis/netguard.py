"""Offline guard: keeps the backend process off the internet and shows what it tried.

``install()`` wraps socket connect, datagram sends and DNS lookups for the
whole backend process. Loopback (the UI, Ollama on 127.0.0.1) and Unix sockets always pass.
Anything else is recorded, and refused while offline mode is on (the default:
all models are on disk, so nothing needs the network). A separate observer
lists live connections of this process and of the Ollama server (a different
process the guard can't wrap) so the dashboard shows what is actually open.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


def is_local(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


class NetGuard:
    def __init__(self, offline: Callable[[], bool] = lambda: True):
        self.offline = offline
        self.attempts: deque[dict] = deque(maxlen=50)
        self._lock = threading.Lock()
        self._installed = False
        self._orig: dict[str, Callable] = {}

    # -- decisions -----------------------------------------------------------------
    def check(self, host: str, port: int | None, what: str) -> None:
        if is_local(host):
            return
        blocked = bool(self.offline())
        with self._lock:
            self.attempts.appendleft({"ts": time.time(), "host": host, "port": port, "what": what, "blocked": blocked})
        if blocked:
            log.warning("offline mode: blocked %s to %s:%s", what, host, port)
            raise OSError(f"offline mode: network access to {host} blocked")
        log.info("network %s to %s:%s (offline mode off)", what, host, port)

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self.attempts)

    # -- process-wide hooks ------------------------------------------------------------
    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        guard = self
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_getaddrinfo = socket.getaddrinfo
        orig_sendto = socket.socket.sendto
        orig_sendmsg = socket.socket.sendmsg
        self._orig = {"connect": orig_connect, "connect_ex": orig_connect_ex, "getaddrinfo": orig_getaddrinfo,
                      "sendto": orig_sendto, "sendmsg": orig_sendmsg,
                      **{n: getattr(socket, n) for n in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr")}}

        def target(sock: socket.socket, address) -> tuple[str, int | None] | None:
            if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple):
                return None  # Unix sockets etc.
            return str(address[0]), int(address[1]) if len(address) > 1 else None

        def connect(sock, address):
            t = target(sock, address)
            if t:
                guard.check(t[0], t[1], "connect")
            return orig_connect(sock, address)

        def connect_ex(sock, address):
            t = target(sock, address)
            if t:
                guard.check(t[0], t[1], "connect")
            return orig_connect_ex(sock, address)

        def getaddrinfo(host, port, *args, **kwargs):
            h = host.decode() if isinstance(host, bytes) else str(host or "")
            if h:
                guard.check(h, port if isinstance(port, int) else None, "dns")
            return orig_getaddrinfo(host, port, *args, **kwargs)

        # datagrams (UDP) reach the network without connect()
        def sendto(sock, data, *args):
            t = target(sock, args[-1]) if args else None
            if t:
                guard.check(t[0], t[1], "send")
            return orig_sendto(sock, data, *args)

        def sendmsg(sock, buffers, *args):
            t = target(sock, args[2]) if len(args) >= 3 else None
            if t:
                guard.check(t[0], t[1], "send")
            return orig_sendmsg(sock, buffers, *args)

        # the older resolver functions don't go through getaddrinfo()
        def lookup(name: str):
            orig = self._orig[name]

            def wrapped(host, *args):
                h = host.decode() if isinstance(host, bytes) else str(host or "")
                if h:
                    guard.check(h, None, "dns")
                return orig(host, *args)

            return wrapped

        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = sendto  # type: ignore[method-assign]
        socket.socket.sendmsg = sendmsg  # type: ignore[method-assign]
        socket.getaddrinfo = getaddrinfo
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            setattr(socket, name, lookup(name))

    def uninstall(self) -> None:  # tests
        if not self._installed:
            return
        socket.socket.connect = self._orig["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = self._orig["sendto"]  # type: ignore[method-assign]
        socket.socket.sendmsg = self._orig["sendmsg"]  # type: ignore[method-assign]
        socket.getaddrinfo = self._orig["getaddrinfo"]
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            setattr(socket, name, self._orig[name])
        self._installed = False


def observe_connections() -> list[dict]:
    """Open non-loopback connections of this process and the Ollama server."""
    try:
        import psutil
    except ImportError:
        return []
    procs = [psutil.Process(os.getpid())]
    try:
        procs += [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").startswith("ollama")]
    except psutil.Error:
        pass
    seen: dict[tuple, dict] = {}
    for p in procs:
        try:
            conns = p.net_connections(kind="inet")
            name = "backend" if p.pid == os.getpid() else "ollama"
        except (psutil.Error, OSError):
            continue
        for c in conns:
            if c.raddr and not is_local(c.raddr.ip):
                key = (name, c.raddr.ip, c.raddr.port)
                seen.setdefault(key, {"process": name, "host": c.raddr.ip, "port": c.raddr.port, "status": c.status,
                                      "count": 0})["count"] += 1
    return list(seen.values())
