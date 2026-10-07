#!/usr/bin/env python3
"""Launch the SWE-Xplorer demo web GUI on localhost.

    python scripts/swe_xplorer.py [--port 8765] [--no-browser]

Same as `python -m minisweagent.gui`, but it switches the console to UTF-8 *before* importing the package,
which avoids UnicodeEncodeError from the startup banner on Windows consoles that use a legacy code page.
"""

import os
import sys

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from minisweagent.gui.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
