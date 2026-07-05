"""Typed configuration loader (plan §15).

Loads ``config.yaml`` into a frozen, validated pydantic model tree. Two
guarantees worth knowing about:

- **Money never touches binary floats.** YAML float scalars (``0.02``,
  ``0.07``…) are constructed directly as ``decimal.Decimal`` from their source
  text via a custom loader, *before* pydantic sees them.
- **Unknown keys are rejected** (``extra="forbid"``), so a typo in
  ``config.yaml`` fails loudly at startup instead of silently using a default.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")


class _DecimalSafeLoader(yaml.SafeLoader):
    """SafeLoader that yields ``Decimal`` (not float) for YAML float scalars.

    Exotic YAML float spellings (``.inf``, ``.nan``) are not valid Decimal
    literals and will raise — they have no business in this config.
    """


def _decimal_constructor(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node))


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _decimal_constructor)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CategoriesConfig(_StrictModel):
    """Category universe per platform (plan §15). v1: geopolitics only."""

    polymarket: list[str] = Field(min_length=1)
    kalshi: list[str] = Field(min_length=1)


class MatchingConfig(_StrictModel):
    """Matching-layer knobs (plan §6)."""

    recall_top_k: int = Field(ge=1)
    recall_min_similarity: float = Field(ge=0.0, le=1.0)
    llm_model: str
    min_confidence: float = Field(ge=0.0, le=1.0)


class EngineConfig(_StrictModel):
    """Signal-engine sizing and alert threshold (plan §3.4, §7)."""

    target_size_pairs: Decimal = Field(gt=0)
    net_threshold_per_pair: Decimal = Field(ge=0)


class PollConfig(_StrictModel):
    """Poll intervals and backoff base (plan §15)."""

    discovery_interval_sec: int = Field(gt=0)
    price_interval_sec: int = Field(gt=0)
    backoff_base_sec: float = Field(gt=0)


class FeesConfig(_StrictModel):
    """Fee-schedule parameters (plan §3). Schedule changes are config edits."""

    kalshi_multiplier_default: Decimal = Field(ge=0, le=1)
    polymarket_fee_rates: dict[str, Decimal]

    @field_validator("polymarket_fee_rates")
    @classmethod
    def _rates_valid(cls, rates: dict[str, Decimal]) -> dict[str, Decimal]:
        normalized: dict[str, Decimal] = {}
        for category, rate in rates.items():
            if not Decimal(0) <= rate <= Decimal(1):
                raise ValueError(
                    f"polymarket fee rate for {category!r} must be in [0, 1], got {rate}"
                )
            normalized[category.lower()] = rate
        return normalized


class AlertingConfig(_StrictModel):
    telegram_enabled: bool


class TrackingConfig(_StrictModel):
    """Tracking-surface locations and retention (plan §9, §15)."""

    state_dir: Path
    status_board_stdout: bool
    sqlite_path: Path
    structured_log_path: Path
    schema_version: int = Field(ge=1)
    keep_dropped_ids: bool
    drop_id_retention_cycles: int = Field(ge=1)


class AppConfig(_StrictModel):
    """Root of the validated configuration tree (mirrors plan §15)."""

    categories: CategoriesConfig
    matching: MatchingConfig
    engine: EngineConfig
    poll: PollConfig
    fees: FeesConfig
    alerting: AlertingConfig
    tracking: TrackingConfig


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate the YAML config at ``path``."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.load(text, Loader=_DecimalSafeLoader)
    return AppConfig.model_validate(raw)
