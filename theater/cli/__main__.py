"""Allow `python -m theater.cli` to work like the `theater` entry point."""

from theater.cli import main

raise SystemExit(main())
