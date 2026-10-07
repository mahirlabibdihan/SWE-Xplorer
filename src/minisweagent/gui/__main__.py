"""Start the SWE-Xplorer demo web GUI:  python -m minisweagent.gui [--port 8765] [--no-browser]"""

import argparse
import os
import sys


def main() -> None:
    # Windows consoles default to cp1252; the agent logs emoji/unicode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

    parser = argparse.ArgumentParser(description="SWE-Xplorer demo GUI (runs on localhost)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()

    from minisweagent.gui.server import serve

    serve("127.0.0.1", args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
