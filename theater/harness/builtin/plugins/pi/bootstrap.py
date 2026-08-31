"""Prepare Pi's process-local startup environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_FILTER = Path(__file__).with_name("pi_startup_filter.cjs")
_EXPECTED_ID_ENV = "THEATER_PI_EXPECTED_NEW_SESSION_ID"


def main() -> None:
    args = sys.argv[1:]
    env = os.environ.copy()
    if len(args) >= 2 and args[0] == "--theater-cold-session-id":
        env[_EXPECTED_ID_ENV] = args[1]
        del args[:2]
        option = f"--require={json.dumps(str(_FILTER))}"
        env["NODE_OPTIONS"] = " ".join(filter(None, (env.get("NODE_OPTIONS"), option)))
    os.execvpe("pi", ["pi", *args], env)


if __name__ == "__main__":
    main()
