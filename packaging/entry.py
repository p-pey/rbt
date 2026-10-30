"""PyInstaller entry point (a script, so the package can be imported normally)."""

import sys

from xauassist.app.main import main

if __name__ == "__main__":
    sys.exit(main())
