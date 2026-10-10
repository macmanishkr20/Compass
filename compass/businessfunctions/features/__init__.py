"""Feature handlers, registered by importing them.

Each module below calls `base.register` at import time, so importing this
package is what makes a handler findable. The registry imports this once
before it validates the catalog — a manifest naming a handler nobody
registered must fail at load, not on the first request.

Adding a feature is a module here plus an entry in some function's manifest.
Neither is enough on its own, which is the point: code with no manifest never
appears, and a manifest with no code does not load.
"""

from __future__ import annotations

from compass.businessfunctions.features import (  # noqa: F401
    form26,
    giftrequests,
    lms,
    oversight,
    rewardlens,
)
from compass.businessfunctions.features.base import (  # noqa: F401
    Feature,
    Figure,
    Outcome,
    Scope,
    Tab,
    get,
    keys,
    register,
)
