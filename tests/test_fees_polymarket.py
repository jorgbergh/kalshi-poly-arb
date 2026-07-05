"""Polymarket per-category fee rates; geopolitics == exactly 0 (plan §3.2, §11 M1, §12)."""

from decimal import Decimal

import pytest

from arbdetector.fees import (
    DEFAULT_FEE_RATES,
    build_fee_registry,
    make_polymarket_fee_model,
    polymarket_taker_fee,
)
from arbdetector.config import load_config
from arbdetector.schema import Platform
from tests.conftest import CONFIG_PATH

D = Decimal


class TestGeopoliticsIsFree:
    """The v1 edge (plan §3.3): the Polymarket leg must cost exactly zero."""

    def test_zero_rate_returns_exactly_zero(self):
        fee = polymarket_taker_fee(D("0.50"), D("100000"), fee_rate=D("0"))
        assert fee == D("0")

    def test_zero_rate_never_hits_min_charge(self):
        # min charge applies only to fee-bearing trades
        assert polymarket_taker_fee(D("0.01"), D("0.001"), fee_rate=D("0")) == D("0")

    def test_geopolitics_in_default_table(self):
        assert DEFAULT_FEE_RATES["geopolitics"] == D("0")

    def test_geopolitics_model_from_registry(self):
        registry = build_fee_registry(load_config(CONFIG_PATH).fees)
        model = registry.get(Platform.POLYMARKET, "geopolitics")
        assert model.fee_fn(D("0.50"), D("500")) == D("0")


class TestCategoryTable:
    """Plan §3.2 table, spot-checked at the 50c peak on 100 shares."""

    @pytest.mark.parametrize(
        ("category", "expected_peak_fee"),
        [
            ("crypto", "1.75"),      # 0.07
            ("economics", "1.25"),   # 0.05
            ("culture", "1.25"),
            ("weather", "1.25"),
            ("other", "1.25"),
            ("finance", "1.00"),     # 0.04
            ("politics", "1.00"),
            ("tech", "1.00"),
            ("mentions", "1.00"),
            ("sports", "0.75"),      # 0.03
            ("geopolitics", "0"),
        ],
    )
    def test_peak_fee_per_category(self, category, expected_peak_fee):
        model = make_polymarket_fee_model(category)
        assert model.fee_fn(D("0.50"), D("100")) == D(expected_peak_fee)

    def test_unknown_category_refused(self):
        with pytest.raises(KeyError):
            make_polymarket_fee_model("horoscopes")

    def test_registry_has_no_polymarket_fallback(self):
        registry = build_fee_registry(load_config(CONFIG_PATH).fees)
        with pytest.raises(KeyError):
            registry.get(Platform.POLYMARKET, "horoscopes")


class TestCurveShape:
    @pytest.mark.parametrize("p", ["0.01", "0.13", "0.25", "0.42", "0.50", "0.99"])
    def test_symmetry(self, p):
        rate = D("0.04")
        assert polymarket_taker_fee(D(p), D("250"), rate) == polymarket_taker_fee(
            D(1) - D(p), D("250"), rate
        )

    def test_tails_vanish_per_share(self):
        # 1000 shares @ 0.01, rate 0.05: 1000*0.05*0.0099 = $0.495 -> $0.000495/share
        fee = polymarket_taker_fee(D("0.01"), D("1000"), D("0.05"))
        assert fee == D("0.495")
        assert fee / 1000 < D("0.001")

    def test_peak_is_maximum(self):
        rate = D("0.05")
        peak = polymarket_taker_fee(D("0.50"), D("1000"), rate)
        for p in ["0.10", "0.30", "0.49", "0.51", "0.70", "0.90"]:
            assert polymarket_taker_fee(D(p), D("1000"), rate) < peak


class TestRounding:
    def test_rounded_to_five_decimals(self):
        # 1 * 0.07 * 0.123 * 0.877 = 0.00755097 -> 0.00755
        assert polymarket_taker_fee(D("0.123"), D("1"), D("0.07")) == D("0.00755")

    def test_min_charge_on_tiny_fee_bearing_trade(self):
        # 0.01 * 0.03 * 0.01 * 0.99 = 0.00000297 -> rounds to 0 -> min 0.00001
        assert polymarket_taker_fee(D("0.01"), D("0.01"), D("0.03")) == D("0.00001")

    def test_price_extremes_are_free_even_with_rate(self):
        assert polymarket_taker_fee(D("0"), D("100"), D("0.07")) == D("0")
        assert polymarket_taker_fee(D("1"), D("100"), D("0.07")) == D("0")


class TestGuards:
    def test_float_inputs_rejected(self):
        with pytest.raises(TypeError):
            polymarket_taker_fee(0.5, D("1"), D("0.04"))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            polymarket_taker_fee(D("0.5"), 1.0, D("0.04"))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            polymarket_taker_fee(D("0.5"), D("1"), 0.04)  # type: ignore[arg-type]

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            polymarket_taker_fee(D("0.5"), D("1"), D("-0.01"))
