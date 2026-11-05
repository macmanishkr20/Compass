"""Write the P&L sample month as files, for trying the upload forms by hand.

    python3 scripts/make_pnl_samples.py <folder>

Writes what `compass/businessfunctions/pnl_sample.py` holds in memory: an FR
workbook per location and service line for Sep and Oct 2026, and one file per
input. Every figure in them is invented. Stores nothing and reads no settings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass.businessfunctions import pnl_sample  # noqa: E402
from compass.businessfunctions.pnl_files import month_label  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    for month in (pnl_sample.SEPTEMBER, pnl_sample.OCTOBER):
        for location, service_line in pnl_sample.FILED:
            name = f"FR {location} {service_line} {month_label(month)}.xlsx"
            (folder / name).write_bytes(pnl_sample.fr_file(month, location, service_line))
    for name, raw in pnl_sample.files().values():
        (folder / name).write_bytes(raw)
    print(f"Wrote {len(list(folder.iterdir()))} files to {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
