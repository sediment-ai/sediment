# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib credential transport policy for the checkout smoke and sim tools."""

import ipaddress
import urllib.request
from urllib.parse import urlsplit

_URL_ERROR = (
    "URL must use HTTPS or loopback HTTP, without credentials, query or fragment"
)


def validate_url(value: str, *, base: bool = False) -> str:
    """Reject ambiguous destinations before credentials or requests are created."""
    if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError(_URL_ERROR)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or "\\" in value
            or parsed.netloc.endswith(":")
            or port == 0
            or (base and parsed.path not in {"", "/"})
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip(".").encode("idna").decode("ascii").split(".")
            if (
                len(host) > 253
                or all(c.isdigit() or c == "." for c in host)
                or not all(
                    label
                    and len(label) <= 63
                    and label[0].isalnum()
                    and label[-1].isalnum()
                    and all(c.isalnum() or c == "-" for c in label)
                    for label in labels
                )
            ):
                raise ValueError
            loopback = host == "localhost"
        else:
            loopback = address.is_loopback
        if parsed.scheme == "http" and not loopback:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError(_URL_ERROR) from None
    return value.rstrip("/") if base else value


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request: urllib.request.Request, *, timeout: float):
    """The final seam also validates callers that bypass entry-point parsing."""
    validate_url(request.full_url)
    return urllib.request.build_opener(RejectRedirects()).open(request, timeout=timeout)
