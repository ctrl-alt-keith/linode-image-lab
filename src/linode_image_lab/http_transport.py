"""HTTP transport that never forwards a credentialed request after a redirect."""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: object, code: int,
                         message: str, headers: object, newurl: str) -> Request:
        raise HTTPError(request.full_url, code, "redirect refused", headers, fp)


def open_without_redirect(request: Request, timeout: float):
    """Open one request; return its response or fail on any HTTP redirect."""
    return build_opener(RejectRedirects()).open(request, timeout=timeout)
