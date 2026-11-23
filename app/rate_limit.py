"""Rate limiting via slowapi.

The limiter has no default limit: a route opts in with `@limiter.limit("...")`
and `app.main` wires the 429 answer. The sign-in route is the one exception:
it counts its attempts itself, with `count_login_attempt` below.

**Whose counter.** Every counter is kept per client address, the address nginx
saw (`security.client_ip`), never a value the client can choose. The key used
to be `request.client.host`, which behind nginx is nginx's own address for
every request: each limit was then one counter for the whole site.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import Request
from limits import RateLimitItem, parse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.wrappers import Limit

from .security import client_ip

log = logging.getLogger(__name__)

# The bucket of a request with no address at all (a test client, a unix
# socket): one shared counter, so such a request is limited, not exempt.
_NO_ADDRESS = "no-address"


def client_key(request: Request) -> str:
    """The limiter's bucket for a request: the client's address as nginx saw
    it. See `security.client_ip` for what that relies on."""
    return client_ip(request) or _NO_ADDRESS


limiter = Limiter(key_func=client_key, default_limits=[])


# ─────────────────────────────── sign-in ───────────────────────────────
# Two counters per quarter of an hour, both kept by client address:
#   LOGIN_PER_ACCOUNT  the attempts on one account from one address;
#   LOGIN_PER_CLIENT   the attempts one address is let through, all accounts
#                      together.
LOGIN_PER_ACCOUNT: RateLimitItem = parse("10/15minute")
LOGIN_PER_CLIENT: RateLimitItem = parse("20/15minute")
_LOGIN_SCOPE = "login"


def count_login_attempt(request: Request, email: str) -> None:
    """Count one sign-in attempt on `email`, or refuse it with a 429.

    Why two counters, and why neither alone:

    - none is kept per account alone: attempts made from elsewhere would then
      lock an account's owner out;
    - the address and the account together are not enough either: one address
      could then try every account without a limit, hence `LOGIN_PER_CLIENT`.

    The order matters. The address's counter is read first and counted last,
    so an attempt refused on its account does not use up the address's
    allowance: an account that keeps failing (a script left with an old
    password) does not close sign-in to the other people behind the same
    address (an office, a mobile operator's gateway). And no account counter
    is created for an address already over its allowance, which bounds the
    counters one address can create.

    A deliberate attempt on several accounts from one address still uses up
    that address's allowance, for everybody behind it: that is what
    `LOGIN_PER_CLIENT` is for.
    """
    if not limiter.enabled:
        return
    address = client_key(request)
    account = _account_key(address, email)
    counters = limiter.limiter
    if not counters.test(LOGIN_PER_CLIENT, _LOGIN_SCOPE, address):
        _refuse(LOGIN_PER_CLIENT, "address")
    if not counters.hit(LOGIN_PER_ACCOUNT, _LOGIN_SCOPE, account):
        _refuse(LOGIN_PER_ACCOUNT, "account")
    if not counters.hit(LOGIN_PER_CLIENT, _LOGIN_SCOPE, address):
        _refuse(LOGIN_PER_CLIENT, "address")


def _account_key(address: str, email: str) -> str:
    """One account as seen from one address. Case and surrounding spaces do
    not make another account of the same e-mail."""
    return f"{address}|{email.strip().casefold()}"


def _refuse(limit: RateLimitItem, counter: str) -> NoReturn:
    """slowapi's own exception, so that `main._rate_limit_handler` answers a
    refused sign-in as it answers a decorated route. The log line names the
    counter, never the address or the account."""
    log.warning("sign-in refused: over %s for one %s", limit, counter)
    raise RateLimitExceeded(
        Limit(
            limit=limit,
            key_func=client_key,
            scope=None,
            per_method=False,
            methods=None,
            error_message=None,
            exempt_when=None,
            cost=1,
            override_defaults=True,
        )
    )
