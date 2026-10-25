"""Estimator — the Compass Estimate module, standing on its own.

Lifted out of Compass rather than rewritten: the engine, the catalog, the
intake and the export are the same code, so an estimate produced here is
the estimate Compass produced, figure for figure. What changed is the
surroundings — one module instead of seven, one router instead of nine,
and only the parts of `common` this one actually reached for.

    estimator/estimate   the module itself: intake, engine, catalog, export
    estimator/common     the subset it depends on, copied whole
    estimator/api        the ASGI app, which mounts exactly one router

WHY A COPY AND NOT A SHARED LIBRARY. The two will drift, and that is the
point of splitting them: this one answers to whoever owns estimating, on
its own release cadence, and a change here cannot break a gift chain in
another product. If they must not drift, do not split them.
"""
