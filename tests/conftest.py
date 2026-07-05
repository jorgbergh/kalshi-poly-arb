"""Shared test constants."""

from pathlib import Path

# The repo's real config.yaml — tests validate against the committed config
# so drift between config and code fails loudly.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
