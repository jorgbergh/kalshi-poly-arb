"""Config loader: typed, strict, and money stays Decimal end-to-end (plan §15)."""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from arbdetector.config import load_config
from tests.conftest import CONFIG_PATH

D = Decimal


class TestRepoConfigLoads:
    def test_loads_and_validates(self):
        cfg = load_config(CONFIG_PATH)
        assert cfg.categories.polymarket == ["geopolitics"]
        assert cfg.matching.min_confidence == 0.80
        assert cfg.tracking.schema_version == 1

    def test_money_is_exact_decimal_never_float(self):
        cfg = load_config(CONFIG_PATH)
        threshold = cfg.engine.net_threshold_per_pair
        assert isinstance(threshold, D)
        assert threshold == D("0.02")  # exact — would fail via float round-trip
        assert isinstance(cfg.fees.kalshi_multiplier_default, D)
        assert cfg.fees.kalshi_multiplier_default == D("0.07")

    def test_geopolitics_rate_is_exactly_zero(self):
        cfg = load_config(CONFIG_PATH)
        rate = cfg.fees.polymarket_fee_rates["geopolitics"]
        assert isinstance(rate, D)
        assert rate == D("0")

    def test_paths_are_paths(self):
        cfg = load_config(CONFIG_PATH)
        assert isinstance(cfg.tracking.state_dir, Path)
        assert cfg.tracking.sqlite_path == Path("state/arb.db")


class TestStrictness:
    def _write(self, tmp_path: Path, mutate: str) -> Path:
        base = CONFIG_PATH.read_text()
        p = tmp_path / "config.yaml"
        p.write_text(base + mutate)
        return p

    def test_unknown_key_rejected(self, tmp_path):
        p = self._write(tmp_path, "\nexecution:\n  enabled: true\n")
        with pytest.raises(ValidationError):
            load_config(p)

    def test_confidence_out_of_range_rejected(self, tmp_path):
        text = CONFIG_PATH.read_text().replace("min_confidence: 0.80", "min_confidence: 1.5")
        p = tmp_path / "config.yaml"
        p.write_text(text)
        with pytest.raises(ValidationError):
            load_config(p)

    def test_fee_rate_out_of_range_rejected(self, tmp_path):
        text = CONFIG_PATH.read_text().replace("crypto: 0.07", "crypto: 1.07")
        p = tmp_path / "config.yaml"
        p.write_text(text)
        with pytest.raises(ValidationError):
            load_config(p)

    def test_fee_rate_categories_normalized_lowercase(self, tmp_path):
        text = CONFIG_PATH.read_text().replace("sports: 0.03", "Sports: 0.03")
        p = tmp_path / "config.yaml"
        p.write_text(text)
        cfg = load_config(p)
        assert cfg.fees.polymarket_fee_rates["sports"] == D("0.03")
