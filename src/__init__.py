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
