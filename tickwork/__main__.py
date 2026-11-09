"""Entry point for ``python -m tickwork`` — delegates to the CLI."""

import sys

from tickwork.cli import main

if __name__ == "__main__":
    sys.exit(main())
