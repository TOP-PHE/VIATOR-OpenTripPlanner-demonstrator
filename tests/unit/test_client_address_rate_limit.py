"""Whose address a request has, and whose counter a rate limit uses.

`security.client_ip` is the address nginx saw (`X-Real-IP`, which nginx
writes itself), never a value from the client's own `X-Forwarded-For`. The
limiter counts by that address, so two clients have two counters and a
client that changes its headers stays in its own. Sign-in keeps two
counters, per address and account and per address
(`rate_limit.count_login_attempt`).

No database: the routes run on a session that finds nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

from app import audit
from app.db import get_db
from app.main import app
from app.rate_limit import (
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_CLIENT,
    _account_key,
    client_key,
    count_login_attempt,
    limiter,
)
from app.security import client_ip

NGINX = "172.18.0.2"  # the peer web sees for every request that came through nginx
CLIENT_A = "203.0.113.7"
CLIENT_B = "198.51.100.23"
FORGED = "192.0.2.99"
PER_ACCOUNT = LOGIN_PER_ACCOUNT.amount
PER_CLIENT = LOGIN_PER_CLIENT.amount


def _request(
    *,
    real_ip: str | None = None,
    forwarded_for: str | None = None,
    peer: str | None = NGINX,
) -> Request:
    headers = []
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode()))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 51234) if peer is not None else None,
        }
    )


# ───────────────────────────── the client's address ─────────────────────────────


def test_the_address_is_the_one_nginx_wrote() -> None:
    assert client_ip(_request(real_ip=CLIENT_A)) == CLIENT_A


@pytest.mark.parametrize(
    "forwarded_for",
    [FORGED, f"{FORGED}, {CLIENT_A}", f"{FORGED}, 10.0.0.1, {CLIENT_A}", "zz-not-an-address"],
)
def test_a_forwarded_for_from_the_client_changes_nothing(forwarded_for: str) -> None:
    request = _request(real_ip=CLIENT_A, forwarded_for=forwarded_for)

    assert client_ip(request) == CLIENT_A
    assert client_key(request) == CLIENT_A


def test_without_nginxs_header_it_is_the_peer_never_the_forwarded_for() -> None:
    """A call made inside the Docker network, not through nginx."""
    request = _request(forwarded_for=FORGED, peer="172.18.0.9")

    assert client_ip(request) == "172.18.0.9"


@pytest.mark.parametrize("real_ip", ["", "zz-not-an-address", "203.0.113.7, 192.0.2.99", "1.2.3"])
def test_a_header_that_is_not_one_address_is_not_used(real_ip: str) -> None:
    assert client_ip(_request(real_ip=real_ip, peer="172.18.0.9")) == "172.18.0.9"


@pytest.mark.parametrize("peer", ["testclient", None])
def test_no_address_at_all_is_none_for_the_audit_and_one_bucket_for_the_limiter(
    peer: str | None,
) -> None:
    request = _request(forwarded_for=FORGED, peer=peer)

    assert client_ip(request) is None  # `audit_events.actor_ip` is `inet NULL`
    assert client_key(request) == "no-address"  # limited, not exempt


def test_an_ipv6_address_is_kept_in_one_spelling() -> None:
    assert client_ip(_request(real_ip="2001:DB8:0:0::0001")) == "2001:db8::1"
    assert client_ip(_request(real_ip=" 203.0.113.7 ")) == CLIENT_A


# ──────────────────────────────── sign-in counters ────────────────────────────────


def _attempt(email: str, address: str = CLIENT_A, **headers: Any) -> None:
    count_login_attempt(_request(real_ip=address, **headers), email)


def _refused(email: str, address: str = CLIENT_A, **headers: Any) -> str:
    """The limit an attempt is refused on, as slowapi names it."""
    with pytest.raises(RateLimitExceeded) as refusal:
        _attempt(email, address, **headers)
    assert refusal.value.status_code == 429
    return str(refusal.value.detail)


def test_the_two_figures() -> None:
    """Changed on purpose only: the route table of docs/architecture.md and
    admin-guide §5.5 quote them."""
    assert str(LOGIN_PER_ACCOUNT) == "10 per 15 minute"
    assert str(LOGIN_PER_CLIENT) == "20 per 15 minute"
    assert PER_ACCOUNT < PER_CLIENT


def test_an_account_is_refused_after_its_attempts_from_one_address() -> None:
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org")

    assert _refused("zz-a@example.org") == str(LOGIN_PER_ACCOUNT)


def test_the_same_account_from_another_address_is_not_refused() -> None:
    """Attempts made from elsewhere do not lock an account's owner out."""
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org", CLIENT_A)
    _refused("zz-a@example.org", CLIENT_A)

    _attempt("zz-a@example.org", CLIENT_B)


def test_another_account_from_the_same_address_is_not_refused() -> None:
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org")
    _refused("zz-a@example.org")

    _attempt("zz-b@example.org")


def test_refused_attempts_on_one_account_leave_the_address_its_allowance() -> None:
    """An account that keeps failing does not close sign-in to the others
    behind the same address."""
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org")
    for _ in range(5 * PER_CLIENT):
        _refused("zz-a@example.org")

    for n in range(PER_CLIENT - PER_ACCOUNT):
        _attempt(f"zz-other-{n}@example.org")
    assert _refused("zz-one-more@example.org") == str(LOGIN_PER_CLIENT)


def test_an_address_is_refused_after_its_attempts_on_all_accounts_together() -> None:
    for n in range(PER_CLIENT):
        _attempt(f"zz-{n}@example.org")

    assert _refused("zz-new@example.org") == str(LOGIN_PER_CLIENT)
    assert _refused("zz-0@example.org") == str(LOGIN_PER_CLIENT)
    _attempt("zz-new@example.org", CLIENT_B)  # another address has its own


def test_an_address_over_its_allowance_creates_no_counter() -> None:
    for n in range(PER_CLIENT):
        _attempt(f"zz-{n}@example.org")

    for n in range(50):
        _refused(f"zz-late-{n}@example.org")

    for n in range(50):
        stats = limiter.limiter.get_window_stats(
            LOGIN_PER_ACCOUNT, "login", _account_key(CLIENT_A, f"zz-late-{n}@example.org")
        )
        assert stats.remaining == PER_ACCOUNT


def test_changing_the_forwarded_for_opens_no_new_allowance() -> None:
    for n in range(PER_ACCOUNT):
        _attempt("zz-a@example.org", forwarded_for=f"192.0.2.{n}")

    assert _refused("zz-a@example.org", forwarded_for="192.0.2.200") == str(LOGIN_PER_ACCOUNT)


@pytest.mark.parametrize("spelling", ["ZZ-A@Example.Org", " zz-a@example.org ", "zz-A@EXAMPLE.org"])
def test_another_spelling_of_the_e_mail_is_the_same_account(spelling: str) -> None:
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org")

    assert _refused(spelling) == str(LOGIN_PER_ACCOUNT)


def test_the_limiters_reset_clears_the_sign_in_counters() -> None:
    """tests/conftest.py resets the limiter before each test: the sign-in
    counters live in the same storage."""
    for _ in range(PER_ACCOUNT):
        _attempt("zz-a@example.org")
    _refused("zz-a@example.org")

    limiter.reset()

    _attempt("zz-a@example.org")


def test_a_limiter_switched_off_counts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limiter, "enabled", False)

    for _ in range(3 * PER_CLIENT):
        _attempt("zz-a@example.org")


# ───────────────────────────── through the routes ─────────────────────────────


class _NoRows:
    """A session that finds nothing: every account is unknown, every token
    invalid."""

    def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def execute(self, *_args: Any, **_kwargs: Any) -> _NoRows:
        return self

    def scalar_one_or_none(self) -> None:
        return None

    def commit(self) -> None:
        """Nothing to write."""


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record", lambda _db, **event: events.append(event))
    return events


@pytest.fixture
def client(audited: list[dict[str, Any]]) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = _NoRows
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _sign_in(
    client: TestClient, email: str, address: str, also: dict[str, str] | None = None
) -> Any:
    """One sign-in attempt as nginx hands it to web, plus the headers a
    client added itself (`also`)."""
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": "zz-wrong-password"},
        headers={"X-Real-IP": address, **(also or {})},
    )


def _confirm(client: TestClient, address: str, also: dict[str, str] | None = None) -> Any:
    """A decorated route (10/hour) that needs no account: an unknown token."""
    return client.post(
        "/api/auth/register-confirm",
        json={"token": "zz-unknown-token", "password": "zz-long-enough-password"},
        headers={"X-Real-IP": address, **(also or {})},
    )


def test_a_failed_sign_in_is_written_with_the_address_nginx_saw(
    client: TestClient, audited: list[dict[str, Any]]
) -> None:
    answer = _sign_in(
        client,
        "zz-a@example.org",
        CLIENT_A,
        {"X-Forwarded-For": f"{FORGED}, {CLIENT_A}"},
    )

    assert answer.status_code == 401
    (event,) = audited
    assert event["action"] == "login.failed"
    assert event["actor_ip"] == CLIENT_A


def test_the_sign_in_route_counts_its_attempts(
    client: TestClient, audited: list[dict[str, Any]]
) -> None:
    statuses = [
        _sign_in(client, "zz-a@example.org", CLIENT_A).status_code for _ in range(PER_ACCOUNT)
    ]
    refused = _sign_in(client, "zz-a@example.org", CLIENT_A)

    assert statuses == [401] * PER_ACCOUNT
    assert refused.status_code == 429
    assert refused.json() == {"detail": f"Rate limit exceeded: {LOGIN_PER_ACCOUNT}"}
    assert refused.headers["Retry-After"] == "60"
    assert len(audited) == PER_ACCOUNT  # a refused attempt reaches no password check
    # Neither the owner elsewhere nor a colleague at the same address is refused.
    assert _sign_in(client, "zz-a@example.org", CLIENT_B).status_code == 401
    assert _sign_in(client, "zz-b@example.org", CLIENT_A).status_code == 401


def test_two_clients_have_two_counters_on_a_decorated_route(client: TestClient) -> None:
    statuses = [_confirm(client, CLIENT_A).status_code for _ in range(10)]

    assert statuses == [400] * 10
    assert _confirm(client, CLIENT_A).status_code == 429
    assert _confirm(client, CLIENT_B).status_code == 400


def test_a_client_cannot_leave_its_counter_by_its_headers(client: TestClient) -> None:
    for n in range(10):
        _confirm(client, CLIENT_A, {"X-Forwarded-For": f"192.0.2.{n}"})

    assert _confirm(client, CLIENT_A, {"X-Forwarded-For": FORGED}).status_code == 429
    assert _confirm(client, CLIENT_A, {"Forwarded": f"for={FORGED}"}).status_code == 429


def test_a_client_cannot_use_up_anothers_counter_by_its_headers(client: TestClient) -> None:
    """The address a client claims in `X-Forwarded-For` is not counted."""
    for _ in range(10):
        _confirm(client, CLIENT_A, {"X-Forwarded-For": CLIENT_B})
    assert _confirm(client, CLIENT_A, {"X-Forwarded-For": CLIENT_B}).status_code == 429

    assert _confirm(client, CLIENT_B).status_code == 400
