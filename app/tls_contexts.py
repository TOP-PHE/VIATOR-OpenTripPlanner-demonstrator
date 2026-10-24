"""Bundled intermediate certificates for hosts that send an incomplete chain (#407).

`user.mobilitaetsverbuende.at`, the Austrian NAP's login server, sends its
leaf certificate (`*.mobilitaetsverbuende.at`) without the intermediate that
signed it, "Go Daddy Secure Certificate Authority - G2". A browser fetches the
missing certificate through the leaf's AIA extension; Python and httpx do not,
so the login failed with `CERTIFICATE_VERIFY_FAILED: unable to get local
issuer certificate`. The fault is the server's; it is reported to
Mobilitätsverbünde Österreich.

The workaround never weakens verification:

- `app/data/tls/extra-intermediates.pem` holds the **public** intermediate,
  as published by GoDaddy (provenance, fingerprint and expiry in the file's
  header and in NOTICE);
- the context of `context_for` is httpx's default context (certifi's roots)
  plus that intermediate, with partial chains refused: the chain must still
  end at certifi's "Go Daddy Root Certificate Authority - G2", host names
  are still checked, and the intermediate only fills the missing link;
- it is used only for the hosts of `INTERMEDIATE_HOSTS`. Every other host
  keeps httpx's default context, and nothing changes the process-wide trust
  store.

Clients that talk to those hosts pass `mounts=tls_mounts()` to
`httpx.AsyncClient`: the listed hosts get a transport with the bundled
context, every other URL the client's own default. Remove the host (and the
PEM file once no host needs it) when the server sends its full chain again —
`openssl s_client -connect <host>:443 -servername <host>` then answers
`Verify return code: 0`.
"""

from __future__ import annotations

import logging
import ssl
import urllib.request
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

EXTRA_INTERMEDIATES_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "data" / "tls" / "extra-intermediates.pem"
)

# The AT NAP flow (docs/nap-feed-resolvers.md): the OAuth2 token URL on
# `user.` (the /credentials "Austria" preset) and the dataset lookups and
# file downloads on `data.` (app/data/eu19_nap_sources.json, OBB). Exact host
# names only — no wildcard, so no other host ever gets the bundled context.
INTERMEDIATE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "user.mobilitaetsverbuende.at",
        "data.mobilitaetsverbuende.at",
    }
)

# A log line (web startup, then daily) once the bundled certificate is this
# close to its expiry (it would then fail those hosts again, with the same error).
EXPIRY_WARNING_DAYS: Final[int] = 60


def build_context(extra_pem: str, cafile: str | None = None) -> ssl.SSLContext:
    """httpx's default verifying context (or one trusting `cafile` only) that
    also knows the certificates of `extra_pem` as chain links.

    `VERIFY_X509_PARTIAL_CHAIN` (set by default since Python 3.13) is cleared:
    with it, OpenSSL would accept a chain ending at a non-self-signed
    certificate of the store, which would make the intermediate a trust
    anchor. Without it the chain must reach a self-signed root of the store.
    """
    ctx = ssl.create_default_context(cafile=cafile) if cafile else httpx.create_ssl_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata=extra_pem)
    ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


@cache
def _bundled_context() -> ssl.SSLContext:
    """The context with the bundled intermediates, built once per process."""
    return build_context(EXTRA_INTERMEDIATES_PATH.read_text(encoding="ascii"))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def context_for(url: str) -> ssl.SSLContext | None:
    """The bundled-intermediate context when `url` is https on a host of
    `INTERMEDIATE_HOSTS`; None — the client's default — for anything else."""
    if urlsplit(url).scheme != "https" or _host(url) not in INTERMEDIATE_HOSTS:
        return None
    return _bundled_context()


def _env_proxy(url: str) -> str | None:
    """The environment's proxy for `url` (HTTPS_PROXY / ALL_PROXY, NO_PROXY
    honoured), so a mounted transport goes the way the client's default
    would."""
    if urllib.request.proxy_bypass(_host(url)):
        return None
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if not proxy:
        return None
    # As httpx does for its own environment proxies: a value without a scheme
    # (`p.example:3128`) means an http:// proxy, never a ValueError.
    return proxy if "://" in proxy else f"http://{proxy}"


def mounts_for(
    hosts: frozenset[str], ctx: ssl.SSLContext
) -> dict[str, httpx.AsyncBaseTransport | None]:
    """httpx mounts that send https requests to `hosts` through a transport
    verifying with `ctx`. A fresh transport per call: each client owns (and
    closes) its connection pool."""
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    for host in sorted(hosts):
        pattern = f"https://{host}"
        mounts[pattern] = httpx.AsyncHTTPTransport(verify=ctx, proxy=_env_proxy(pattern))
    return mounts


def tls_mounts() -> dict[str, httpx.AsyncBaseTransport | None]:
    """`httpx.AsyncClient(mounts=tls_mounts())`: the hosts of
    `INTERMEDIATE_HOSTS` verify with the bundled intermediate, every other
    URL with the client's default context.

    Every refresh client calls this (any provider, the OSM refresh, NAP
    catalogue imports, Test login), so the PEM file must ship with the app:
    moving or dropping `EXTRA_INTERMEDIATES_PATH` breaks all refreshes, not
    only the AT ones."""
    return mounts_for(INTERMEDIATE_HOSTS, _bundled_context())


def expiry_warning(now: datetime | None = None) -> str | None:
    """A message when a bundled certificate expires within
    `EXPIRY_WARNING_DAYS` (or is unreadable), else None."""
    from cryptography import x509

    now = now or datetime.now(UTC)
    try:
        certs = x509.load_pem_x509_certificates(EXTRA_INTERMEDIATES_PATH.read_bytes())
    except (OSError, ValueError) as exc:
        return f"tls.extra_intermediates_unreadable type={type(exc).__name__}"
    for cert in certs:
        not_after = cert.not_valid_after_utc
        if not_after - now <= timedelta(days=EXPIRY_WARNING_DAYS):
            return (
                f"tls.extra_intermediate_expiring subject={cert.subject.rfc4514_string()} "
                f"not_after={not_after.date().isoformat()} file={EXTRA_INTERMEDIATES_PATH.name}"
            )
    return None


def log_expiry_warning() -> None:
    """One WARNING when `expiry_warning` has something to say: at web startup
    and from the daily scheduler job (`app/main.py`)."""
    message = expiry_warning()
    if message is not None:
        log.warning(message)
