"""Deterministic IDs: same inputs -> same ids across restarts (plan §9.2, §12)."""

import hashlib

import pytest

from arbdetector.schema import Direction, Platform
from arbdetector.tracking import entity_id, opp_id, pair_id

KALSHI_EID = entity_id(Platform.KALSHI, "FED-25DEC-T3.00")
POLY_EID = entity_id(Platform.POLYMARKET, "0xdeadbeef")


class TestEntityId:
    def test_matches_spec_formula(self):
        expected = hashlib.sha1(b"kalshi:FED-25DEC-T3.00").hexdigest()[:8]
        assert KALSHI_EID == expected

    def test_deterministic_across_calls(self):
        assert entity_id(Platform.KALSHI, "FED-25DEC-T3.00") == KALSHI_EID

    def test_enum_and_string_platform_agree(self):
        assert entity_id("polymarket", "0xdeadbeef") == POLY_EID

    def test_length_and_charset(self):
        assert len(KALSHI_EID) == 8
        assert set(KALSHI_EID) <= set("0123456789abcdef")

    def test_platform_disambiguates(self):
        assert entity_id(Platform.KALSHI, "X") != entity_id(Platform.POLYMARKET, "X")

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            entity_id("coinbase", "X")


class TestPairId:
    def test_matches_spec_formula(self):
        expected = hashlib.sha1(f"{KALSHI_EID}:{POLY_EID}".encode()).hexdigest()[:8]
        assert pair_id(KALSHI_EID, POLY_EID) == expected

    def test_deterministic(self):
        assert pair_id(KALSHI_EID, POLY_EID) == pair_id(KALSHI_EID, POLY_EID)

    def test_argument_order_matters(self):
        # kalshi-first is the fixed convention; swapping would silently fork ids
        assert pair_id(KALSHI_EID, POLY_EID) != pair_id(POLY_EID, KALSHI_EID)

    def test_length(self):
        assert len(pair_id(KALSHI_EID, POLY_EID)) == 8


class TestOppId:
    PID = pair_id(KALSHI_EID, POLY_EID)
    TS = "2026-07-05T14:32:07Z"

    def test_deterministic_and_length_12(self):
        a = opp_id(self.PID, Direction.YES_KALSHI_NO_POLY, self.TS)
        b = opp_id(self.PID, Direction.YES_KALSHI_NO_POLY, self.TS)
        assert a == b
        assert len(a) == 12

    def test_direction_string_and_enum_agree(self):
        assert opp_id(self.PID, "YES@kalshi+NO@poly", self.TS) == opp_id(
            self.PID, Direction.YES_KALSHI_NO_POLY, self.TS
        )

    def test_direction_and_timestamp_disambiguate(self):
        a = opp_id(self.PID, Direction.YES_KALSHI_NO_POLY, self.TS)
        assert a != opp_id(self.PID, Direction.NO_KALSHI_YES_POLY, self.TS)
        assert a != opp_id(self.PID, Direction.YES_KALSHI_NO_POLY, "2026-07-05T14:32:12Z")

    def test_freeform_direction_rejected(self):
        with pytest.raises(ValueError):
            opp_id(self.PID, "YES@poly+NO@kalshi", self.TS)
