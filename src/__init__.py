# -*- coding: utf-8 -*-
"""Package bootstrap for the daily analysis entrypoint."""

from __future__ import annotations

import os
import sys


# ``sitecustomize.py`` lives at the repository root.  Python does not import a
# repository-local sitecustomize early enough for ``python main.py`` on every
# runner, but by the time ``src`` is imported the script directory is on
# sys.path.  Load it explicitly only for the main daily-analysis entrypoint so
# library/test imports keep their normal behavior.
if os.path.basename(sys.argv[0] or "") == "main.py":
    try:
        import sitecustomize  # noqa: F401
    except ImportError:
        pass

    # Fork-local runtime extensions are kept separate from upstream modules so
    # they remain easy to review/rebase: Exa MCP news search plus balanced
    # leading/lagging sector coverage for brief Enterprise WeChat reports.
    try:
        from src.opencode_go_extensions import install as _install_opencode_go_extensions

        _install_opencode_go_extensions()
    except ImportError:
        pass

    # The hosted keyless Exa tool currently returns publication metadata as a
    # ``Published:`` line in its text payload. Install the small compatibility
    # provider after the generic extension so dated news survives the project's
    # existing freshness filter.
    try:
        from src.opencode_go_exa_public_fix import install as _install_public_exa_fix

        _install_public_exa_fix()
    except ImportError:
        pass

    # US market sector breadth: use the 11 Select Sector SPDR ETFs as liquid
    # GICS-sector proxies and inject deterministic leading/lagging blocks into
    # the concise market review.
    try:
        from src.us_sector_extension import install as _install_us_sector_extension

        _install_us_sector_extension()
    except ImportError:
        pass
