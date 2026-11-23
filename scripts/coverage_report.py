# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Coverage floor of the self-pipeline: 100% on Linux, the reference platform (pyproject.toml);
99% elsewhere, where the POSIX-only lines can't run (known issue L18)."""

import subprocess
import sys

floor = "100" if sys.platform.startswith("linux") else "99"
sys.exit(subprocess.run([sys.executable, "-m", "coverage", "report", f"--fail-under={floor}"], check=False).returncode)
