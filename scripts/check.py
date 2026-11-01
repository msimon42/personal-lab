"""Required offline release checks; no browser, game or model credentials."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = [
    [sys.executable, "-m", "ruff", "check", "backend", "packages", "apps", "tests", "scripts", "setup.py"],
    [sys.executable, "-m", "mypy"],
    [sys.executable, "scripts/generate_contracts.py", "--check"],
    [sys.executable, "-m", "pytest"],
    [sys.executable, "-m", "personal_lab", "fixtures"],
]

if __name__ == "__main__":
    for command in COMMANDS:
        subprocess.run(command, cwd=ROOT, check=True)
