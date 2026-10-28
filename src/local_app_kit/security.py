"""Reject requests that other websites make through the user's browser.

Listening on 127.0.0.1 is not enough: any page open in the browser can send requests to the app
(CSRF), or point a domain of its own at 127.0.0.1 and read the answers (DNS rebinding). This
middleware only lets through requests addressed to an allowed host name and coming either from
the app's own pages or from outside a browser (curl, scripts), which send no `Origin`.
"""

from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EXPOSURES = ("local", "public")
CSP = "default-src 'self'"


def allowed_hosts(exposure: str = "local", public_host: str | None = None) -> frozenset[str]:
    """Host names the app answers to at an exposure level.

    `local`: only this machine. `public`: also `public_host`, the domain of the tunnel (ngrok)
    that reaches the app through a login.
    """
    if exposure not in EXPOSURES:
        raise ValueError(f"unknown exposure {exposure!r}; expected one of {EXPOSURES}")
    if exposure == "local":
        return LOCAL_HOSTS
    if not public_host:
        raise ValueError("exposure 'public' needs public_host, the tunnel's domain")
    return LOCAL_HOSTS | {_hostname(public_host)}


def _hostname(value: str) -> str:
    """`Host` header or URL to bare host name: `[::1]:8000` -> `::1`."""
    if "//" not in value:
        value = "//" + value
    return (urlsplit(value).hostname or "").lower()


class SecurityMiddleware:
    """Checks `Host`, `Origin` and `Sec-Fetch-Site`, and adds a `Content-Security-Policy`."""

    def __init__(self, app: ASGIApp, hosts: frozenset[str] = LOCAL_HOSTS) -> None:
        self.app = app
        self.hosts = hosts

    def rejection(self, headers: Headers) -> str | None:
        if _hostname(headers.get("host", "")) not in self.hosts:
            return "host not allowed"
        origin = headers.get("origin")
        # "null" is what sandboxed frames and some redirects send: never ours.
        if origin is not None and _hostname(origin) not in self.hosts:
            return "origin not allowed"
        site = headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return "request from another site"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self.rejection(Headers(scope=scope))
        if reason:
            if scope["type"] == "websocket":
                await WebSocketClose(code=1008, reason=reason)(scope, receive, send)
            else:
                response = PlainTextResponse(reason, status_code=403, headers={"Content-Security-Policy": CSP})
                await response(scope, receive, send)
            return

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("Content-Security-Policy", CSP)
            await send(message)

        await self.app(scope, receive, send_with_csp)
