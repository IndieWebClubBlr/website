"""SSRF protection for every URL the site fetches.

Feed URLs, entry permalinks, favicons and member pages are all controlled
by somebody other than us: a feed we fetch can point us at a service that
only the build host can reach (a cloud metadata endpoint, a service on the
build host's LAN). Every outbound request therefore goes through
:func:`safe_get` or :func:`safe_head`, which refuse URLs that do not
resolve to a public address.

The check mirrors FeedRepeat's SSRF guard: the host name is rejected when
it is a known private name, the host is resolved, and the request is
refused when *any* of the resolved addresses is private or reserved. As
there, the name is resolved again by the HTTP client, so a host that
answers differently for the two lookups (DNS rebinding) is not fully
covered.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

# Schemes we are willing to fetch. Anything else (file://, gopher://, ...)
# is refused before a request is made.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# How many redirects to follow. Every hop is checked separately. The cap
# is the one requests applies, so a chain that used to work still works.
MAX_REDIRECTS = 30

# Host names that never point at a public address.
_PRIVATE_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "0.0.0.0",
        "::",
    }
)

# Ranges that are not publicly routable but that Python's ipaddress module
# does not consider private. On Python 3.13 and later is_global() already
# covers everything except RFC 6598 (carrier-grade NAT), which is only
# load-bearing on older versions.
_EXTRA_IPV4_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)

# Deprecated site-local addresses (RFC 3879). Python does not classify
# them as private, but they are not routable either.
_EXTRA_IPV6_NETWORKS = (ipaddress.ip_network("fec0::/10"),)

# The interface identifiers that mark an ISATAP address (RFC 5214), which
# embeds an IPv4 address in the last 32 bits. The set is the union of the
# forms RFC 5214 lists and the ones feed-repeat lists, so both are caught.
_ISATAP_MARKERS = frozenset(
    {
        0x00005EFE,  # 0:0:5efe
        0x01005EFE,  # 100:5efe
        0x02005EFE,  # 200:5efe
        0x04005EFE,  # 400:5efe
        0x40005EFE,  # 4000:5efe
        0x42005EFE,  # 4200:5efe
    }
)

# The public addresses of the hosts resolved so far, and the lock that
# guards them.
_RESOLVED: dict[str, list[str]] = {}
_RESOLVE_LOCK = threading.Lock()


class SSRFError(requests.RequestException):
    """Raised when a URL must not be fetched.

    It is a request error, so the call sites that already handle failed
    requests (a feed that cannot be fetched, a newsletter archive that
    falls back to its cache) handle a refusal the same way.
    """


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address embedded in an IPv6 address.

    Mirrors FeedRepeat's extraction: IPv4-mapped (::ffff:0:0/96), NAT64
    (64:ff9b::/96), 6to4 (2002::/16), Teredo (2001:0::/32, the client
    address) and ISATAP interface identifiers.
    """
    words = ip.packed
    w1, w2, w3, w4 = (int.from_bytes(words[i : i + 4], "big") for i in (0, 4, 8, 12))

    embedded: list[int] = []

    # ISATAP: an interface identifier of the form xxxx:5efe:IPv4.
    if w3 in _ISATAP_MARKERS:
        embedded.append(w4)

    # IPv4-mapped: ::ffff:a.b.c.d
    if w1 == 0 and w2 == 0 and w3 == 0xFFFF:
        embedded.append(w4)

    # NAT64 well-known prefix: 64:ff9b::/96
    if w1 == 0x0064FF9B and w2 == 0 and w3 == 0:
        embedded.append(w4)

    # 6to4: 2002::/16, the IPv4 address sits in the next 32 bits.
    if w1 & 0xFFFF0000 == 0x20020000:
        embedded.append(((w1 & 0xFFFF) << 16) | (w2 >> 16))

    # Teredo: 2001:0::/32, the client IPv4 is the last word, inverted.
    if w1 == 0x20010000:
        embedded.append(w4 ^ 0xFFFFFFFF)

    return [ipaddress.IPv4Address(value) for value in embedded]


def is_public_address(address: str) -> bool:
    """Whether an IP address is a public, routable address.

    Args:
        address: An IPv4 or IPv6 address in its textual form.

    Returns:
        True when the address is publicly routable.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in network for network in _EXTRA_IPV6_NETWORKS):
            return False
        # Translation and tunnelling prefixes are refused as a whole: NAT64
        # (64:ff9b::/96) is reserved and 6to4/Teredo are not global, so this
        # refuses them even when the embedded IPv4 address is public. That
        # deliberately includes NAT64, which means an IPv6-only network that
        # reaches the internet through a NAT64 gateway cannot fetch anything.
        if (
            ip.is_multicast
            or ip.is_reserved
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
        ):
            return False
        # An address that embeds an IPv4 one reaches that IPv4 host, so
        # both the address itself and the embedded one have to be public.
        embedded = _embedded_ipv4(ip)
        if embedded and not all(is_public_address(str(each)) for each in embedded):
            return False
        return ip.is_global

    if any(ip in network for network in _EXTRA_IPV4_NETWORKS):
        return False
    if (
        ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


def _resolve_addresses(host: str) -> list[str]:
    """Resolve a host name to the addresses it points at.

    Args:
        host: The host name to resolve.

    Returns:
        The resolved addresses, in the order the resolver returned them.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _public_addresses(host: str) -> list[str] | None:
    """Resolve a host name and return its addresses, if they are all public.

    Only successful lookups are kept, so a temporary DNS failure is retried.
    The client resolves the name again when it connects, so a host whose DNS
    answers differently for the two lookups (DNS rebinding) is not covered.

    Args:
        host: The host name to resolve.

    Returns:
        The resolved addresses, an empty list when the host resolves but at
        least one address is not public, or None when the host does not
        resolve at all.
    """
    with _RESOLVE_LOCK:
        cached = _RESOLVED.get(host)
    if cached is not None:
        return cached

    addresses = _resolve_addresses(host)
    if not addresses:
        return None
    if not all(is_public_address(address) for address in addresses):
        return []

    with _RESOLVE_LOCK:
        _RESOLVED[host] = addresses
    return addresses


def _url_refusal(url: str) -> str | None:
    """Return why a URL must not be fetched, or None when it may be.

    Args:
        url: The URL to check.

    Returns:
        A human-readable reason for refusing the URL, or None.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return "it cannot be parsed"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return f"its scheme is not one of {sorted(ALLOWED_SCHEMES)}"
    if not host:
        return "it has no host"

    name = host.lower().rstrip(".")
    if name in _PRIVATE_NAMES or name.endswith(".localhost"):
        return f"the host {name!r} is a known private name"

    addresses = _public_addresses(name)
    if addresses is None:
        return f"the host {name!r} could not be resolved"
    if not addresses:
        return f"the host {name!r} does not resolve to a public address"
    return None


def is_public_url(url: str) -> bool:
    """Whether a URL points at a public, routable address.

    Args:
        url: The URL to check.

    Returns:
        True when the URL is safe to fetch.
    """
    return _url_refusal(url) is None


def _prepare(
    session: requests.Session, method: str, url: str
) -> requests.PreparedRequest:
    """Build the request requests would send, to read the URL it will dial.

    ``urllib.parse`` and the parser requests uses do not always agree on a
    URL (a backslash before the ``@``, for one), so the raw string is not
    what gets fetched. Preparing the request yields the URL that requests
    normalizes and that urllib3 then uses to pick the connection host.

    Args:
        session: The session that will send the request.
        method: The HTTP method to use.
        url: The URL to fetch.

    Returns:
        The prepared request, which is not sent.

    Raises:
        SSRFError: If requests cannot prepare the URL.
    """
    try:
        return session.prepare_request(requests.Request(method, url))
    except (requests.RequestException, ValueError) as error:
        raise SSRFError(f"refusing to fetch an invalid URL: {url}") from error


def safe_request(
    session: requests.Session, method: str, url: str, **kwargs: Any
) -> requests.Response:
    """Perform a request, refusing URLs that do not point at a public address.

    Redirects are always followed by hand so that every hop is checked: a
    public URL may well redirect to an internal one. ``allow_redirects`` is
    therefore ignored. Credentials attached to the session (``session.auth``
    or an ``Authorization`` header) are reapplied to every hop, including
    redirects to another host, so pass a session without credentials when
    the URLs come from somebody else.

    Args:
        session: The session to use for the request.
        method: The HTTP method to use.
        url: The URL to fetch.
        **kwargs: Passed on to the session's request method.

    Returns:
        The final response.

    Raises:
        SSRFError: If the URL (or a redirect target) is not public, or the
            redirect chain is too long.
    """
    current = url
    kwargs.pop("allow_redirects", None)
    for _ in range(MAX_REDIRECTS + 1):
        prepared = _prepare(session, method, current)
        prepared_url = prepared.url or ""
        refusal = _url_refusal(prepared_url)
        if refusal is not None:
            logger.debug(f"Refusing to fetch {prepared_url}: {refusal}")
            raise SSRFError(f"refusing to fetch {prepared_url}: {refusal}")

        response = session.request(method, current, allow_redirects=False, **kwargs)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise SSRFError(f"redirect without a location: {current}")
            current = urljoin(current, location)
            continue
        return response

    raise SSRFError(f"too many redirects while fetching {url}")


def safe_get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    """Fetch a URL with GET, refusing anything that is not publicly reachable.

    Args:
        session: The session to use for the request.
        url: The URL to fetch.
        **kwargs: Passed on to the session's request method.

    Returns:
        The final response.

    Raises:
        SSRFError: If the URL (or a redirect target) is not public.
    """
    return safe_request(session, "GET", url, **kwargs)


def safe_head(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    """Fetch a URL with HEAD, refusing anything that is not publicly reachable.

    Args:
        session: The session to use for the request.
        url: The URL to fetch.
        **kwargs: Passed on to the session's request method.

    Returns:
        The final response.

    Raises:
        SSRFError: If the URL (or a redirect target) is not public.
    """
    return safe_request(session, "HEAD", url, **kwargs)
