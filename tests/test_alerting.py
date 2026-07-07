"""Alerting: message content, both sinks, and the de-dup alert stage (§8, §9)."""

from decimal import Decimal

import httpx
import pytest

from arbdetector.alerting import run_alert
from arbdetector.alerting.console import ConsoleAlerter
from arbdetector.alerting.format import format_opportunity
from arbdetector.alerting.telegram import TelegramAlerter
from arbdetector.store.sqlite import Store
from arbdetector.tracking import DropReason, Stage
from arbdetector.tracking.ids import matched_pair_id
from arbdetector.tracking.runstate import opportunity_summary
from tests.test_runstate_serialization import make_opportunity

D = Decimal
TS = "2026-07-07T12:00:00+00:00"


class TestFormatter:
    def test_carries_every_required_field(self):
        text = format_opportunity(opportunity_summary(make_opportunity()),
                                  is_update=False, plain=True)
        # plan §8 mandatory content
        assert "Will KX-1 happen?" in text and "Will 0x1 happen?" in text
        assert "NO@kalshi+YES@poly" in text
        assert "$+0.0486" in text and "5.10%" in text and "500" in text
        assert "0.0060" in text and "0.9416" in text  # fills
        assert "0 / 2.10" in text                     # per-leg fees
        assert "evaluation times differ by 2h" in text  # LLM caveats
        assert "confidence 0.85" in text

    def test_plain_strips_ansi_update_header(self):
        text = format_opportunity(opportunity_summary(make_opportunity()),
                                  is_update=True, plain=True)
        assert "\033[" not in text
        assert "ARB UPDATE" in text

    def test_colored_default_has_ansi(self):
        text = format_opportunity(opportunity_summary(make_opportunity()),
                                  is_update=False)
        assert "\033[" in text and "ARB OPPORTUNITY" in text


class TestConsoleAlerter:
    def test_prints(self, capsys):
        ConsoleAlerter().send(opportunity_summary(make_opportunity()), is_update=False)
        assert "ARB OPPORTUNITY" in capsys.readouterr().out


class TestTelegramAlerter:
    def make(self, handler, **kw):
        client = httpx.Client(transport=httpx.MockTransport(handler),
                             base_url="https://tg.test")
        return TelegramAlerter(bot_token="TOK", chat_id="42", http_client=client, **kw)

    def test_posts_to_sendmessage_with_credentials(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["json"] = __import__("json").loads(request.content)
            return httpx.Response(200, json={"ok": True})

        self.make(handler).send(opportunity_summary(make_opportunity()), is_update=False)
        assert seen["path"] == "/botTOK/sendMessage"
        assert seen["json"]["chat_id"] == "42"
        assert "ARB OPPORTUNITY" in seen["json"]["text"]
        assert "\033[" not in seen["json"]["text"]  # plain for Telegram

    def test_blank_credentials_disable_no_network(self):
        def handler(request):
            raise AssertionError("must not call the API when disabled")

        alerter = TelegramAlerter(
            bot_token="", chat_id="",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert alerter.enabled is False
        alerter.send(opportunity_summary(make_opportunity()), is_update=False)  # no-op

    def test_http_error_propagates(self):
        alerter = self.make(lambda r: httpx.Response(400, json={"ok": False}))
        with pytest.raises(httpx.HTTPStatusError):
            alerter.send(opportunity_summary(make_opportunity()), is_update=False)


class RecordingAlerter:
    name = "rec"
    enabled = True

    def __init__(self, fail: bool = False):
        self.sent = []
        self._fail = fail

    def send(self, summary, *, is_update):
        if self._fail:
            raise RuntimeError("sink down")
        self.sent.append((summary["pair_id"], is_update))


class TestRunAlertDedup:
    def _store(self, tmp_path):
        store = Store(tmp_path / "arb.db", schema_version=2)
        store.begin_cycle(TS)
        return store

    def test_new_opportunity_is_sent(self, tmp_path):
        store = self._store(tmp_path)
        sink = RecordingAlerter()
        opp = make_opportunity()
        emitted, result, errors = run_alert(
            [opp], store=store, alerters=[sink],
            material_delta=D("0.005"), cycle_id=1, ts=TS,
        )
        assert len(emitted) == 1 and errors == 0
        assert sink.sent == [(matched_pair_id(opp.pair), False)]
        assert result.stage is Stage.ALERT
        assert result.n_in == 1 and result.n_out == 1 and result.drops == {}
        # recorded for next-cycle de-dup
        assert store.last_alert(matched_pair_id(opp.pair), opp.direction.value) is not None
        store.close()

    def test_unchanged_repeat_is_duplicate(self, tmp_path):
        store = self._store(tmp_path)
        opp = make_opportunity()
        run_alert([opp], store=store, alerters=[RecordingAlerter()],
                  material_delta=D("0.005"), cycle_id=1, ts=TS)
        sink = RecordingAlerter()
        emitted, result, _ = run_alert(
            [opp], store=store, alerters=[sink],
            material_delta=D("0.005"), cycle_id=2, ts=TS,
        )
        assert emitted == [] and sink.sent == []
        assert result.n_out == 0
        assert result.drops == {DropReason.DUPLICATE: 1}
        assert sum(result.drops.values()) == result.n_in - result.n_out
        store.close()

    def test_material_change_re_alerts_as_update(self, tmp_path):
        store = self._store(tmp_path)
        opp = make_opportunity()
        run_alert([opp], store=store, alerters=[RecordingAlerter()],
                  material_delta=D("0.005"), cycle_id=1, ts=TS)
        moved = ArbOpportunity_with_net(opp, D("0.0486") + D("0.01"))
        sink = RecordingAlerter()
        emitted, result, _ = run_alert(
            [moved], store=store, alerters=[sink],
            material_delta=D("0.005"), cycle_id=2, ts=TS,
        )
        assert len(emitted) == 1
        assert sink.sent == [(matched_pair_id(opp.pair), True)]  # is_update=True
        store.close()

    def test_sub_material_move_is_still_duplicate(self, tmp_path):
        store = self._store(tmp_path)
        opp = make_opportunity()
        run_alert([opp], store=store, alerters=[RecordingAlerter()],
                  material_delta=D("0.005"), cycle_id=1, ts=TS)
        nudged = ArbOpportunity_with_net(opp, D("0.0486") + D("0.001"))  # < delta
        _, result, _ = run_alert(
            [nudged], store=store, alerters=[RecordingAlerter()],
            material_delta=D("0.005"), cycle_id=2, ts=TS,
        )
        assert result.drops == {DropReason.DUPLICATE: 1}
        store.close()

    def test_send_failure_counted_not_dropped(self, tmp_path):
        store = self._store(tmp_path)
        opp = make_opportunity()
        emitted, result, errors = run_alert(
            [opp], store=store, alerters=[RecordingAlerter(fail=True)],
            material_delta=D("0.005"), cycle_id=1, ts=TS,
        )
        # genuinely new -> emitted + recorded (won't spam next cycle), but the
        # failed delivery is surfaced as an error, never a silent DUPLICATE
        assert len(emitted) == 1 and errors == 1
        assert result.drops == {}
        assert store.last_alert(matched_pair_id(opp.pair), opp.direction.value) is not None
        store.close()

    def test_disabled_alerters_skipped(self, tmp_path):
        store = self._store(tmp_path)
        off = RecordingAlerter()
        off.enabled = False
        emitted, result, errors = run_alert(
            [make_opportunity()], store=store, alerters=[off],
            material_delta=D("0.005"), cycle_id=1, ts=TS,
        )
        assert len(emitted) == 1 and off.sent == [] and errors == 0
        store.close()


def ArbOpportunity_with_net(opp, net):
    """Copy an opportunity with a new net_per_pair (same pair/direction)."""
    from dataclasses import replace

    return replace(opp, net_per_pair=net)
