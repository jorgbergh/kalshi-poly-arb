"""Kalshi fee curve: peak at 0.50, symmetry, tails -> ~0 (plan §3.1, §11 M1, §12).

The ceil-to-cent applies once PER ORDER, so per-contract properties are
asserted on order sizes where rounding is negligible (or exact).
"""

from decimal import Decimal

import pytest

from arbdetector.fees import (
    DEFAULT_KALSHI_MULTIPLIER,
    build_fee_registry,
    kalshi_taker_fee,
    make_kalshi_fee_model,
)
from arbdetector.config import load_config
from arbdetector.schema import Platform
from tests.conftest import CONFIG_PATH

D = Decimal


class TestPeak:
    def test_peak_per_contract_is_0_0175_at_half(self):
        # 100 contracts @ 0.50: 0.07 * 0.25 * 100 = $1.75 exactly (no rounding)
        fee = kalshi_taker_fee(D("0.50"), D("100"))
        assert fee == D("1.75")
        assert fee / 100 == D("0.0175")

    def test_single_contract_rounds_up_per_order(self):
        # raw $0.0175 -> ceil to the cent PER ORDER -> $0.02
        assert kalshi_taker_fee(D("0.50"), D("1")) == D("0.02")

    def test_half_is_the_maximum_of_the_curve(self):
        # The per-order ceil rounds to the cent, so prices whose raw fee is
        # within a cent of the peak (e.g. 0.49/0.51 at this size) may TIE the
        # peak after rounding — they can never exceed it.
        peak = kalshi_taker_fee(D("0.50"), D("1000"))
        for p in ["0.01", "0.10", "0.25", "0.40", "0.75", "0.99"]:
            assert kalshi_taker_fee(D(p), D("1000")) < peak
        for p in ["0.49", "0.51"]:
            assert kalshi_taker_fee(D(p), D("1000")) <= peak


class TestSymmetry:
    @pytest.mark.parametrize("p", ["0.01", "0.13", "0.25", "0.42", "0.50", "0.87"])
    @pytest.mark.parametrize("n", ["1", "10", "137", "500"])
    def test_fee_at_p_equals_fee_at_one_minus_p(self, p, n):
        assert kalshi_taker_fee(D(p), D(n)) == kalshi_taker_fee(D(1) - D(p), D(n))

    def test_known_value_with_rounding(self):
        # 0.07 * 0.13 * 0.87 * 137 = 1.084629 -> ceil to cent -> $1.09
        assert kalshi_taker_fee(D("0.13"), D("137")) == D("1.09")


class TestTails:
    def test_per_contract_fee_vanishes_at_tails(self):
        # 1000 contracts @ 0.01: raw 0.693 -> $0.70 -> $0.0007/contract
        fee = kalshi_taker_fee(D("0.01"), D("1000"))
        assert fee == D("0.70")
        per_contract = fee / 1000
        assert D("0") < per_contract < D("0.001")

    def test_price_extremes_are_free(self):
        assert kalshi_taker_fee(D("0"), D("100")) == 0
        assert kalshi_taker_fee(D("1"), D("100")) == 0

    def test_zero_size_is_free(self):
        assert kalshi_taker_fee(D("0.50"), D("0")) == 0


class TestConfigurableMultiplier:
    def test_multiplier_is_a_parameter_not_a_constant(self):
        # plan §3.1: special-event markets carry different multipliers
        assert kalshi_taker_fee(D("0.50"), D("100"), multiplier=D("0.035")) == D("0.88")

    def test_default_multiplier_matches_plan(self):
        assert DEFAULT_KALSHI_MULTIPLIER == D("0.07")


class TestGuards:
    def test_float_price_rejected(self):
        with pytest.raises(TypeError):
            kalshi_taker_fee(0.5, D("1"))  # type: ignore[arg-type]

    def test_float_size_rejected(self):
        with pytest.raises(TypeError):
            kalshi_taker_fee(D("0.5"), 100.0)  # type: ignore[arg-type]

    def test_price_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            kalshi_taker_fee(D("1.01"), D("1"))
        with pytest.raises(ValueError):
            kalshi_taker_fee(D("-0.01"), D("1"))

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError):
            kalshi_taker_fee(D("0.5"), D("-1"))


class TestRegistryIntegration:
    def test_kalshi_platform_default_covers_any_category(self):
        registry = build_fee_registry(load_config(CONFIG_PATH).fees)
        model = registry.get(Platform.KALSHI, "World")
        assert model.platform is Platform.KALSHI
        assert model.fee_fn(D("0.50"), D("100")) == D("1.75")

    def test_fee_model_binds_multiplier(self):
        model = make_kalshi_fee_model("special", multiplier=D("0.14"))
        # 0.14 * 0.25 * 100 = $3.50
        assert model.fee_fn(D("0.50"), D("100")) == D("3.50")
