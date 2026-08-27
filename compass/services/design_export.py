"""Moved to compass.design.export.

Kept as an import path because this is where the rest of Compass has
always looked. Re-exports everything, so `from compass.services import
design_export` behaves exactly as it did.
"""

from __future__ import annotations

from compass.design.export import *  # noqa: F401,F403
from compass.design import export as _moved

# `import *` skips names with a leading underscore, and several of these
# are imported by name elsewhere; carry the whole namespace across.
globals().update({k: v for k, v in vars(_moved).items()
                  if k not in ("__name__", "__doc__", "__package__",
                               "__loader__", "__spec__", "__file__")})
