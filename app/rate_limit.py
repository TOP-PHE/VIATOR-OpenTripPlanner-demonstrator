"""Per-route rate limiting via slowapi.

Configured with a no-default limiter; routes opt in with `@limiter.limit("...")`.
The exception handler is wired in `app.main`.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# `key_func` is what slowapi uses as the bucket identifier. `get_remote_address`
# respects X-Forwarded-For when the app is behind a trusted proxy (we'll set
# `app.state.limiter` and `request.client.host` works through Starlette).
limiter = Limiter(key_func=get_remote_address, default_limits=[])
