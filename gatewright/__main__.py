# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Allows `python -m gatewright ...` (same as the `gatewright` command)."""

import sys
from .cli import main

sys.exit(main())
