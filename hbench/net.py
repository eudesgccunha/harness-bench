"""The network rules every outgoing request follows: https (http only for localhost), no redirects, a total
deadline and a cap on the reply that is read. `check_litellm_url` (the engine) and the OTLP sink share them."""

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from email.message import Message
from typing import IO

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_REPLY_BYTES = 1_000_000


class NetError(Exception):
    """A request failed in a way the caller should report: an oversized reply."""


def url_allowed(url: str) -> bool:
    """Whether `url` is https, or http to localhost."""
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in LOCAL_HOSTS)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a header (an Authorization key) must not reach another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def _exchange(request: urllib.request.Request, timeout: float, abandoned: threading.Event) -> bytes:
    """Send `request` and read the reply, at most `MAX_REPLY_BYTES`; stops reading once `abandoned` is set."""
    chunks: list[bytes] = []
    size = 0
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as r:
            while chunk := r.read1(min(65_536, MAX_REPLY_BYTES + 1 - size)):  # one socket read at a time
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_REPLY_BYTES:
                    raise NetError("the reply is too large")
                if abandoned.is_set():
                    raise TimeoutError("the reply took too long")
    except urllib.error.HTTPError as e:
        e.close()  # an error status holds the socket open until it is closed
        raise
    return b"".join(chunks)


def post_bounded(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
    """POST `body` to `url` and return the reply. The URL must pass `url_allowed` (`ValueError` otherwise), no
    redirect is followed, the whole exchange takes at most `timeout` seconds (`TimeoutError`) and the reply is
    at most `MAX_REPLY_BYTES` (`NetError`). Any other failure (HTTP status, connection) is raised as it is.

    `timeout` alone limits each socket operation, so a server that drips the status line, the headers or the
    body never times out. The exchange runs in a daemon thread (it cannot keep the process alive) and the caller
    waits at most `timeout` for it."""
    if not url_allowed(url):
        raise ValueError("the URL must use https (http only for localhost)")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    outcome: list[bytes | BaseException] = []
    abandoned = threading.Event()

    def exchange() -> None:
        try:
            outcome.append(_exchange(request, timeout, abandoned))
        except BaseException as e:  # handed to the caller, which re-raises it
            outcome.append(e)

    worker = threading.Thread(target=exchange, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not outcome:
        abandoned.set()  # the thread stops at its next read; its sockets time out on their own
        raise TimeoutError("the reply took too long")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result
