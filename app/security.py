"""Response security headers and the two static-file mounts."""

from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders


# Every directive is 'self' or 'none': Inter is self-hosted from /fonts, so a
# visitor's browser makes no third-party request of any kind. That is a claim
# the privacy policy makes explicitly - if you ever add an external font, CDN
# or script here, update /privacy-policy in the same change.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": _CSP,
}


class SecurityHeadersMiddleware:
    """Inject security headers at response-start. Pure ASGI (not
    BaseHTTPMiddleware) so it never buffers the body - the SSE chat stream
    keeps flushing chunks as they arrive."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles for Astro's /_astro bundles. Their filenames are content
    hashed, so a given URL never changes meaning - safe to cache hard. The
    browser refetches only when the hash (and thus the URL) changes."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        return response


class FontStaticFiles(StaticFiles):
    """StaticFiles for the self-hosted webfonts. Their names are stable rather
    than content-hashed, so they can't be marked immutable - a week of caching
    keeps repeat visits free while still letting a font swap land."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
        return response
