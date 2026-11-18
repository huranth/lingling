import select
import socket
import threading
import time

from . import settings


def cut(sock):
    # then wake
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def ms(started):
    return int((time.monotonic() - started) * settings.MS_SECOND)


class Proxy:
    def __init__(self, source, events=None):
        self.lane = source
        self.events = events
        self.server = None
        self.port = None
        self.lock = threading.Lock()
        self.active = set()
        self.generation = 0

    def start(self):
        family = socket.AF_INET6 if ":" in settings.PROXY_HOST else socket.AF_INET
        self.server = socket.socket(family, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((settings.PROXY_HOST, 0))
        self.server.listen(settings.PROXY_BACKLOG)
        self.port = self.server.getsockname()[1]

        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()

    def stop(self):
        if self.server:
            self.server.close()
            self.server = None

        with self.lock:
            pairs = list(self.active)
            self.active.clear()

        for left, right in pairs:
            cut(left)
            cut(right)

    def switch(self, lane):
        # then move
        with self.lock:
            self.lane = lane
            self.generation += 1
            pairs = list(self.active)
            self.active.clear()

        for left, right in pairs:
            cut(left)
            cut(right)

    @property
    def url(self):
        host = settings.PROXY_HOST
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        return f"http://{host}:{self.port}"

    def _loop(self):
        while True:
            try:
                client, _ = self.server.accept()
            except Exception:
                return

            thread = threading.Thread(
                target=self._handle,
                args=(client,),
                daemon=True,
            )
            thread.start()

    def _handle(self, client):
        remote = None
        host = ""

        with self.lock:
            lane = self.lane
            generation = self.generation

        port = 0
        started = time.monotonic()
        failed = False
        opened = False

        try:
            head = b""
            while b"\r\n\r\n" not in head:
                if len(head) > settings.PROXY_MAX_HEAD:
                    client.sendall(b"HTTP/1.1 431 Header Too Large\r\n\r\n")
                    return

                part = client.recv(settings.PROXY_HEAD_CHUNK)
                if not part:
                    return

                head += part

            if len(head) > settings.PROXY_MAX_HEAD:
                client.sendall(b"HTTP/1.1 431 Header Too Large\r\n\r\n")
                return

            line = head.split(b"\r\n", 1)[0].decode("latin1")
            method, target, _ = line.split(" ", 2)

            if method != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return

            host, port = target.rsplit(":", 1)
            host = host.strip("[]")
            port = int(port)

            remote = self._connect(host, port, lane)

            if not self._track(client, remote, generation):
                remote.close()
                return

            family = "ipv6" if remote.family == socket.AF_INET6 else "ipv4"
            self._note("open", lane, host, port, note=family)
            opened = True
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._pipe(client, remote)
        except Exception as exc:
            failed = True
            if opened:
                self._note("close", lane, host, port, ms(started), "reset")
            else:
                self._note("fail", lane, host, port, ms(started), str(exc))
        finally:
            self._untrack(client, remote)
            try:
                client.close()
            except OSError:
                pass
            if remote:
                try:
                    remote.close()
                except OSError:
                    pass
            if not failed and host:
                self._note("close", lane, host, port, ms(started))

    def _connect(self, host, port, lane):
        # then addresses
        infos = socket.getaddrinfo(
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
        fault = None

        for family, kind, proto, _, addr in infos:
            # then v6
            if family != socket.AF_INET6:
                continue

            sock = socket.socket(family, kind, proto)

            try:
                sock.bind((lane, 0))
                sock.connect(addr)
                return sock
            except OSError as exc:
                fault = exc
                sock.close()

        if fault:
            raise fault

        raise OSError("no ipv6 address")

    def _pipe(self, left, right):
        # then relay
        while True:
            ready, _, _ = select.select([left, right], [], [], settings.PROXY_SELECT)
            if not ready:
                continue

            for sock in ready:
                data = sock.recv(settings.PROXY_CHUNK)
                if not data:
                    return

                other = right if sock is left else left
                other.sendall(data)

    def _track(self, client, remote, generation):
        # then pair
        with self.lock:
            if generation != self.generation:
                return False
            self.active.add((client, remote))
            return True

    def _untrack(self, client, remote):
        with self.lock:
            self.active.discard((client, remote))

    def _note(self, kind, lane, host, port, elapsed=0, note=""):
        if self.events is None:
            return

        try:
            self.events(kind, lane, host, port, elapsed, note)
        except Exception:
            pass
