"""Phase 2 diagnosis. The verdict is code, so this is where it gets pinned."""

from __future__ import annotations

import json

import pytest

from oncallbot.config import Config, HraConfig
from oncallbot.diagnose import (
    INDETERMINATE,
    MATCH,
    MISMATCH,
    RUNBOOK_GENDER,
    RUNBOOK_NAME,
    Blocked,
    compare_gender,
    compare_name,
    diagnose_order,
    gather,
    gender_history,
    normalize_gender,
    normalize_name,
    read_report_identity,
)
from oncallbot.hra_client import HraAuthError, HraClient, HraError


# --- normalization ---------------------------------------------------------


def test_name_normalization_ignores_only_case_and_outer_space():
    assert normalize_name("  Asha Menon  ") == "asha menon"
    assert normalize_name("ASHA MENON") == normalize_name("asha menon")
    assert normalize_name("Asha  Menon") == "asha menon"       # collapsed
    assert normalize_name(None) == ""


def test_name_comparison_treats_an_honorific_as_a_mismatch():
    """The runbook is explicit: an extra 'Mr.' stops the doctor summary."""
    assert not compare_name("Mr. Ravi Kumar", "Ravi Kumar").matches
    assert not compare_name("Ravi Kumar", "Ravi K Kumar").matches
    assert not compare_name("Ravi Kumar", "Ravi Kumar.").matches
    assert compare_name("RAVI KUMAR", "ravi kumar").matches      # case only


@pytest.mark.parametrize(
    "given,expected",
    [("m", "m"), ("M", "m"), ("male", "m"), ("MALE", "m"),
     ("f", "f"), ("Female", "f"), ("", ""), (None, ""), ("unknown", "")],
)
def test_gender_normalization(given, expected):
    assert normalize_gender(given) == expected


def test_gender_comparison_normalizes_across_representations():
    """fetch_all_orders says "male", get_user_details says "m"."""
    assert compare_gender("male", "m").matches
    assert compare_gender("Female", "f").matches
    assert not compare_gender("male", "f").matches


def test_gender_comparison_keeps_raw_values_for_the_evidence_block():
    c = compare_gender("male", "m")
    assert (c.report_value, c.record_value) == ("male", "m")
    assert (c.report_normalized, c.record_normalized) == ("m", "m")


def test_unrecognised_gender_never_counts_as_a_match():
    assert not compare_gender("", "m").matches
    assert not compare_gender("other", "m").matches
    assert not compare_gender("m", "").matches


# --- report parsing --------------------------------------------------------


def test_reads_the_new_report_format():
    got = read_report_identity({"PatientName": "Asha Menon", "Gender": "female"})
    assert got == {"name": "Asha Menon", "gender": "female", "format": "new"}


def test_reads_the_old_report_format():
    got = read_report_identity(
        {"TestReports": [{"PName": "Asha Menon", "Gender": "F", "PatientID": "9"}]}
    )
    assert got["name"] == "Asha Menon"
    assert got["gender"] == "F"
    assert got["format"] == "old"


def test_reads_a_bare_list_as_the_old_format():
    assert read_report_identity([{"PName": "Ravi", "Gender": "M"}])["format"] == "old"


def test_unknown_report_shape_is_flagged_not_guessed():
    assert read_report_identity({"foo": 1})["format"] == "unknown"
    assert read_report_identity([])["format"] == "unknown"
    assert read_report_identity(None)["format"] == "unknown"


# --- version history -------------------------------------------------------


def _versions(*rows):
    return {"versions": list(rows)}


def test_gender_history_finds_creation_value_and_the_edit():
    payload = _versions(
        {
            "whodunnit": "agent-1",
            "created_at": "2026-08-20 10:00:00",
            "object": {"gender": "m", "name": "Ravi"},
            "object_changes": {"gender": "f", "updated": "2026-08-20 10:00:00"},
        },
    )
    created, changes = gender_history(payload)
    assert created == "m"
    assert len(changes) == 1
    assert (changes[0].from_value, changes[0].to_value) == ("m", "f")
    assert changes[0].changed_at == "2026-08-20 10:00:00"
    assert changes[0].actor_id == "agent-1"


def test_gender_history_ignores_rows_that_did_not_touch_gender():
    payload = _versions(
        {"object": {"gender": "m"}, "object_changes": {"name": "Ravi K"}},
        {"object": {"gender": "m"}, "object_changes": {"date_of_birth": "2000-01-01"}},
    )
    created, changes = gender_history(payload)
    assert created == "m"
    assert changes == []


def test_gender_history_orders_changes_chronologically():
    payload = _versions(
        {"object": {"gender": "f"}, "object_changes": {"gender": "m", "updated": "2026-08-22"}},
        {"object": {"gender": "m"}, "object_changes": {"gender": "f", "updated": "2026-08-20"}},
    )
    _created, changes = gender_history(payload)
    assert [c.changed_at for c in changes] == ["2026-08-20", "2026-08-22"]


def test_gender_history_tolerates_an_empty_or_odd_payload():
    assert gender_history({}) == ("", [])
    assert gender_history(None) == ("", [])
    assert gender_history(_versions("junk", None)) == ("", [])


# --- the resolution chain --------------------------------------------------

OGID = "PO10003583002-668"


class StubClient:
    """Stands in for HraClient. Every hop can be overridden or made to fail."""

    def __init__(self, **over):
        self.calls: list[str] = []
        self.over = over

    def _o(self, key, default):
        v = self.over.get(key, default)
        if isinstance(v, Exception):
            raise v
        return v

    def user_details(self, ogid):
        self.calls.append("user_details")
        return self._o("user_details", {
            "user_id": "u-1",
            "patient_details": [
                {"id": "p-sibling", "name": "Other Person", "gender": "f"},
                {"id": "p-1", "name": "Ravi Kumar", "gender": "m"},
            ],
        })

    def orders(self, user_id, ogid=None):
        self.calls.append("orders")
        return self._o("orders", [
            {"order_group_id": OGID, "booking_id": "b-1", "patient_id": "p-1",
             "delivery_time": "2026-08-20"},
        ])

    def json_report_url(self, booking_id, ogid):
        self.calls.append("json_report_url")
        return self._o("json_report_url", {"json_url": "https://s3/report.json?sig=x"})

    def fetch_json_report(self, url):
        self.calls.append("fetch_json_report")
        return self._o("report", {"PatientName": "Ravi Kumar", "Gender": "male"})

    def patient_versions(self, patient_id):
        self.calls.append("patient_versions")
        return self._o("versions", _versions(
            {"whodunnit": "phlebo-9",
             "object": {"gender": "m", "name": "Ravi K"},
             "object_changes": {"name": "Ravi Kumar", "updated": "2026-08-19"}},
        ))

    def close(self):
        pass


def _thread(subject: str = "Report missing", body: str = "Call 9876543210 please."):
    """A minimal EmailThread for the thread-aware paths."""
    from datetime import datetime, timezone

    from oncallbot.models import EmailMessage, EmailThread

    m = EmailMessage(
        id="m1", thread_id="t1",
        date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        sender="Asha <asha@x.com>", to="health-record-support@1mg.com", cc="",
        subject=subject, body_text=body, snippet=body[:30],
    )
    return EmailThread(id="t1", subject=subject, messages=[m])


def _cfg():
    c = Config()
    c.hra = HraConfig(token_env="TEST_TOKEN_UNUSED")
    return c


def test_chain_selects_the_bookings_patient_not_the_first_one():
    """Comparing against a sibling's record is the bug class we are diagnosing."""
    facts = gather(StubClient(), OGID)
    assert facts["patient_id"] == "p-1"
    assert facts["patient"]["name"] == "Ravi Kumar"


def test_chain_visits_every_hop_in_order():
    c = StubClient()
    gather(c, OGID)
    assert c.calls == [
        "user_details", "orders", "json_report_url",
        "fetch_json_report", "patient_versions",
    ]


def test_chain_blocks_when_the_booking_has_no_patient_id():
    c = StubClient(orders=[{"order_group_id": OGID, "booking_id": "b-1"}])
    with pytest.raises(Blocked, match="carries a patient_id"):
        gather(c, OGID)


def test_chain_blocks_when_the_patient_is_not_on_the_account():
    c = StubClient(orders=[{"order_group_id": OGID, "booking_id": "b-1",
                            "patient_id": "p-unknown"}])
    with pytest.raises(Blocked, match="refusing to compare"):
        gather(c, OGID)


def test_chain_marks_an_expired_token_distinctly():
    c = StubClient(user_details=HraAuthError("token expired"))
    with pytest.raises(Blocked) as e:
        gather(c, OGID)
    assert e.value.auth is True


def test_chain_blocks_on_a_transport_failure_without_claiming_auth():
    c = StubClient(json_report_url=HraError("network down"))
    with pytest.raises(Blocked) as e:
        gather(c, OGID)
    assert e.value.auth is False


# --- end-to-end verdicts ---------------------------------------------------


def test_matching_record_yields_match_and_no_runbook():
    d = diagnose_order(_cfg(), OGID, client=StubClient())
    assert d.verdict == MATCH
    assert d.runbook == ""
    assert d.resolution == []


def test_name_mismatch_is_diagnosed_with_rendered_steps():
    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.verdict == MISMATCH
    assert d.runbook == RUNBOOK_NAME
    assert d.patient_id == "p-1"
    assert d.resolution[1] == 'Click "Edit patient" and set the name to exactly `Mr. Ravi Kumar`.'
    assert OGID in d.resolution[2]
    assert all(s[0].isupper() for s in d.resolution), "steps start with an action verb"


def test_gender_mismatch_is_diagnosed_with_the_timeline():
    c = StubClient(
        report={"PatientName": "Ravi Kumar", "Gender": "female"},
        versions=_versions(
            {"whodunnit": "agent-7",
             "object": {"gender": "f"},
             "object_changes": {"gender": "m", "updated": "2026-08-21 09:00:00"}},
        ),
    )
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.verdict == MISMATCH
    assert d.runbook == RUNBOOK_GENDER
    assert d.gender_created_as == "f"
    assert d.gender_changes[0].to_value == "m"
    assert d.gender_changes[0].actor_id == "agent-7"
    assert "set gender to `f`" in d.resolution[1]


def test_name_is_checked_before_gender_when_both_differ():
    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "female"})
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.runbook == RUNBOOK_NAME, "name is the documented cause of a missing summary"


def test_a_blocked_chain_returns_indeterminate_rather_than_raising():
    c = StubClient(json_report_url={"json_url": ""})
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.verdict == INDETERMINATE
    assert "digitisation record" in d.blocked_because
    assert d.runbook == ""


def test_an_expired_token_is_indeterminate_and_flagged_as_auth():
    """A stale token must never read as a real inconclusive verdict."""
    c = StubClient(user_details=HraAuthError("The admin API rejected the token (401)."))
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.verdict == INDETERMINATE
    assert d.auth_expired is True
    assert "rejected the token" in d.blocked_because


def test_diagnosis_records_the_trail_it_walked():
    d = diagnose_order(_cfg(), OGID, client=StubClient())
    joined = " | ".join(d.trail)
    assert "user_id u-1" in joined
    assert "booking(s) on this order" in joined
    assert "new format" in joined


def test_diagnosis_serializes_for_the_store_and_ui():
    d = diagnose_order(_cfg(), OGID, client=StubClient())
    blob = json.dumps(d.to_dict())
    assert "comparisons" in blob and "verdict" in blob


# --- client behaviour ------------------------------------------------------


def test_client_raises_auth_error_on_401(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(401, text="unauthorized")

    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg, client=httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url + cfg.path_prefix,
    ))
    with pytest.raises(HraAuthError, match="expire"):
        c.user_details(OGID)


def test_client_unwraps_a_torpedo_data_envelope(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, json={"data": {"user_id": "u-9"}, "statusCode": 200})

    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg, client=httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url + cfg.path_prefix,
    ))
    assert c.user_details(OGID) == {"user_id": "u-9"}


def test_client_reports_a_non_json_body_clearly(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, text="<html>login</html>")

    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg, client=httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url + cfg.path_prefix,
    ))
    with pytest.raises(HraError, match="not JSON"):
        c.user_details(OGID)


def test_missing_token_names_the_fix(monkeypatch):
    from oncallbot.config import ConfigError

    monkeypatch.delenv("ONCALLBOT_HRA_TOKEN", raising=False)
    with pytest.raises(ConfigError) as e:
        HraConfig().token()
    assert "localStorage" in str(e.value)
    assert ".env" in str(e.value)


def test_diagnosis_does_not_call_a_model_by_default(monkeypatch):
    """The verdict is code. Prose is opt-in, so unit paths stay offline."""
    def boom(*a, **k):
        raise AssertionError("diagnose_order must not call a model by default")

    monkeypatch.setattr("oncallbot.diagnose.write_reason", boom)
    d = diagnose_order(_cfg(), OGID, client=StubClient())
    assert d.verdict == MATCH
    assert d.reason == ""


def test_reason_is_written_when_asked_and_never_decides_the_verdict(monkeypatch):
    monkeypatch.setattr(
        "oncallbot.diagnose.write_reason",
        lambda cfg, d, subject="": f"Narrated {d.verdict} for {d.order_group_id}.",
    )
    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c, with_reason=True)
    assert d.verdict == MISMATCH          # unchanged by the narration
    assert d.reason == f"Narrated mismatch for {OGID}."


def test_a_failed_reason_call_does_not_lose_the_verdict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("oncallbot.diagnose.write_reason", boom)
    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c, with_reason=True)
    assert d.verdict == MISMATCH
    assert d.reason == ""
    assert any("reason unavailable" in t for t in d.trail)


def test_reason_prompt_carries_the_evidence_and_forbids_re_deciding():
    from oncallbot.diagnose import REASON_SYSTEM_PROMPT, build_reason_prompt

    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c)
    prompt = build_reason_prompt(d, subject="Smart report not generated")

    assert "Verdict: mismatch" in prompt
    assert "'Mr. Ravi Kumar'" in prompt and "'Ravi Kumar'" in prompt
    assert "untrusted" in prompt          # the ticket subject is fenced
    flat = " ".join(REASON_SYSTEM_PROMPT.split())
    assert "The verdict is settled" in flat
    assert "Never invent" in flat


def test_edge_block_is_not_reported_as_an_auth_problem(monkeypatch):
    """Cloudflare 1106 also answers 403; blaming the token wastes a cycle."""
    import httpx

    from oncallbot.config import HraConfig
    from oncallbot.hra_client import HraBlockedError

    def handler(request):
        return httpx.Response(
            403,
            headers={"server": "cloudflare"},
            json={"title": "Error 1106: Access denied",
                  "detail": "The site owner has blocked your IP address"},
        )

    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg, client=httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url + cfg.path_prefix,
    ))
    with pytest.raises(HraBlockedError) as e:
        c.user_details(OGID)
    assert "VPN" in str(e.value)
    assert "never checked" in str(e.value)


def test_a_real_403_from_the_service_still_reads_as_permission(monkeypatch):
    import httpx

    from oncallbot.config import HraConfig

    def handler(request):
        return httpx.Response(403, json={"error": "role not permitted"})

    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg, client=httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cfg.base_url + cfg.path_prefix,
    ))
    with pytest.raises(HraAuthError, match="lacks permission"):
        c.user_details(OGID)


# --- the diagnose CLI command ----------------------------------------------


def _mock_hra(monkeypatch, report, versions=None, orders=None):
    """Point HraClient at a MockTransport, stubbing the bare report fetch."""
    import httpx

    from oncallbot import hra_client as hc

    rows = orders if orders is not None else [
        {"order_group_id": OGID, "booking_id": "b-9001", "patient_id": "p-42",
         "delivery_time": "2026-08-15 18:00:00"}
    ]

    def handler(request):
        path = request.url.path
        if path.endswith(f"/users/order/{OGID}"):
            return httpx.Response(200, json={"data": {
                "user_id": "u-77",
                "patient_details": [
                    {"id": "p-sib", "name": "Other Person", "gender": "f"},
                    {"id": "p-42", "name": "Ravi Kumar", "gender": "m"},
                ]}})
        if path.endswith("/orders"):
            return httpx.Response(200, json={"data": rows})
        if "/json-report" in path:
            return httpx.Response(200, json={"data": {"json_url": "https://s3.test/r.json"}})
        if "/versions" in path:
            return httpx.Response(200, json={"data": versions or {"versions": []}})
        return httpx.Response(404, json={"error": path})

    real = hc.HraClient   # captured before patching, or factory recurses

    def factory(cfg, client=None):
        c = real(cfg)
        c.client = httpx.Client(transport=httpx.MockTransport(handler),
                                base_url=cfg.base_url + cfg.path_prefix)
        c.fetch_json_report = lambda _u: report
        return c

    monkeypatch.setenv("ONCALLBOT_HRA_TOKEN", "test-token")
    monkeypatch.setattr(hc, "HraClient", factory)


def _run(tmp_path, args):
    from typer.testing import CliRunner

    from oncallbot import cli

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gmail:\n  account: bot@1mg.com\n"
        f"store:\n  path: {tmp_path / 'x.db'}\n"
        "categories: [wrong_patient_mapping, data_correction, other]\n"
    )
    return CliRunner().invoke(cli.app, [*args, "--config", str(cfg)], catch_exceptions=False)


def test_cli_reports_a_name_mismatch_with_steps(tmp_path, monkeypatch):
    _mock_hra(monkeypatch, {"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    r = _run(tmp_path, ["diagnose", OGID])
    assert r.exit_code == 0
    assert "MISMATCH" in r.output and "name_mismatch" in r.output
    # The CLI strips the backticks that the chat UI turns into code.
    assert "set the name to exactly Mr. Ravi Kumar" in r.output
    assert "p-42" in r.output


def test_cli_reports_a_gender_mismatch_with_the_timeline(tmp_path, monkeypatch):
    _mock_hra(
        monkeypatch,
        {"PatientName": "Ravi Kumar", "Gender": "female"},
        versions=_versions({
            "whodunnit": "phlebo-31",
            "object": {"gender": "m"},
            "object_changes": {"gender": "f", "updated": "2026-08-14 11:20:05"},
        }),
    )
    r = _run(tmp_path, ["diagnose", OGID])
    assert "gender_mismatch" in r.output
    assert "Gender history" in r.output
    assert "phlebo-31" in r.output


def test_cli_reports_a_match_and_offers_no_steps(tmp_path, monkeypatch):
    _mock_hra(monkeypatch, {"PatientName": "Ravi Kumar", "Gender": "male"})
    r = _run(tmp_path, ["diagnose", OGID])
    assert r.exit_code == 0
    assert "MATCH" in r.output
    assert "Resolution" not in r.output


def test_cli_exits_nonzero_and_explains_when_indeterminate(tmp_path, monkeypatch):
    """A booking with no patient_id must not be forced into a verdict."""
    _mock_hra(
        monkeypatch,
        {"PatientName": "Ravi Kumar", "Gender": "male"},
        orders=[{"order_group_id": OGID, "booking_id": "b-1"}],
    )
    r = _run(tmp_path, ["diagnose", OGID])
    assert r.exit_code == 1
    assert "INDETERMINATE" in r.output
    assert "carries a patient_id" in r.output
    assert "Got as far as" in r.output


def test_cli_json_output_is_machine_readable(tmp_path, monkeypatch):
    _mock_hra(monkeypatch, {"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    r = _run(tmp_path, ["diagnose", OGID, "--format", "json"])
    payload = json.loads(r.output[r.output.index("{"):r.output.rindex("}") + 1])
    assert payload["verdict"] == "mismatch"
    assert payload["runbook"] == "name_mismatch"
    assert len(payload["resolution"]) == 3


def test_cli_does_not_narrate_unless_asked(tmp_path, monkeypatch):
    import oncallbot.diagnose as dg

    def boom(*a, **k):
        raise AssertionError("the CLI must not call a model without --reason")

    monkeypatch.setattr(dg, "write_reason", boom)
    _mock_hra(monkeypatch, {"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    assert _run(tmp_path, ["diagnose", OGID]).exit_code == 0


def test_cli_raw_mode_dumps_payloads_and_names_the_booking_keys(tmp_path, monkeypatch):
    """--raw is for the first live run: it shows the real key names."""
    _mock_hra(monkeypatch, {"PatientName": "Ravi Kumar", "Gender": "male"})
    r = _run(tmp_path, ["diagnose", OGID, "--raw"])
    assert "GET /users/order/" in r.output
    assert "booking row keys:" in r.output
    assert "booking_id" in r.output


def test_a_blocked_chain_keeps_the_trail_it_walked():
    """How far it got is the most useful thing when a diagnosis stops."""
    c = StubClient(orders=[{"order_group_id": OGID, "booking_id": "b-1"}])
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.verdict == INDETERMINATE
    assert d.trail, "the trail must survive the block"
    assert any("user_id u-1" in t for t in d.trail)


# --- version history, against the shape the live API actually returns -------


def test_actor_comes_from_row_level_whodunnit():
    """The live payload has no metadata.actor_id; the actor is `whodunnit`."""
    from oncallbot.diagnose import field_history

    _created, changes = field_history(
        _versions({
            "whodunnit": "user-42",
            "object": {"name": "Champa Paul"},
            "object_changes": {"name": "Ujjwal Das", "updated": "2026-08-13 19:12:27"},
        }),
        "name",
    )
    assert changes[0].actor_id == "user-42"


def test_creation_value_is_the_oldest_row_not_the_first():
    """Rows arrive newest-first, and `object` holds only changed fields."""
    from oncallbot.diagnose import gender_history

    created, changes = gender_history(_versions(
        {"whodunnit": "u", "created_at": "2026-08-13 19:13:33",
         "object": {"updated": "…"}, "object_changes": {"updated": "…"}},
        {"whodunnit": "u", "created_at": "2026-08-13 19:12:27",
         "object": {"gender": "f", "name": "Champa Paul"},
         "object_changes": {"gender": "m", "name": "Ujjwal Das",
                            "updated": "2026-08-13 19:12:27"}},
    ))
    assert created == "f", "the value before the oldest edit"
    assert [(c.from_value, c.to_value) for c in changes] == [("f", "m")]


def test_name_history_surfaces_a_repurposed_patient_id():
    """The real failure that neither runbook catches."""
    from oncallbot.diagnose import name_history

    created, changes = name_history(_versions(
        {"whodunnit": "u-1",
         "object": {"name": "Champa Paul"},
         "object_changes": {"name": "Ujjwal Das", "updated": "2026-08-13 19:12:27"}},
    ))
    assert created == "Champa Paul"
    assert changes[0].to_value == "Ujjwal Das"
    assert changes[0].field == "name"


def test_field_history_falls_back_when_nothing_ever_changed_it():
    from oncallbot.diagnose import field_history

    created, changes = field_history(_versions(
        {"whodunnit": "u", "created_at": "2026-01-01",
         "object": {"gender": "m"}, "object_changes": {"updated": "x"}},
    ), "gender")
    assert changes == []
    assert created == "m"


def test_diagnosis_carries_both_timelines():
    c = StubClient(report={"PatientName": "Ravi Kumar", "Gender": "female"})
    d = diagnose_order(_cfg(), OGID, client=c)
    assert d.name_changes and d.name_changes[0].to_value == "Ravi Kumar"
    assert d.name_created_as == "Ravi K"


# --- intelligent diagnosis: checks, triage, findings ------------------------


def test_checks_report_both_runbooks_pass_or_fail():
    from oncallbot.diagnose import RUNBOOK_GENDER, RUNBOOK_NAME

    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c)

    assert [x.runbook for x in d.checks] == [RUNBOOK_NAME, RUNBOOK_GENDER]
    assert [x.passed for x in d.checks] == [False, True]
    assert "Mr. Ravi Kumar" in d.checks[0].detail
    assert "Ravi Kumar" in d.checks[0].detail
    assert "'m'" in d.checks[1].detail


def test_every_check_is_reported_even_when_all_pass():
    """A reader needs to see what was ruled out, not only what fired."""
    d = diagnose_order(_cfg(), OGID, client=StubClient())
    assert len(d.checks) == 2
    assert all(c.passed for c in d.checks)
    assert all(c.label for c in d.checks)


def test_resolution_steps_backtick_identifiers():
    from oncallbot.diagnose import strip_code_marks

    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = diagnose_order(_cfg(), OGID, client=c)
    assert "`p-1`" in d.resolution[0]
    assert f"`{OGID}`" in d.resolution[2]
    assert "`" not in strip_code_marks(d.resolution[0])


def _fake_prose(monkeypatch, text="", *, expect="findings", plan=None, followup=""):
    """Stub the one backend seam every prose and planning call goes through.

    Which system prompt it was handed says what the code was doing, so the
    sequence is recorded. The follow-up planner is answered with `plan`, which
    defaults to "nothing documented can answer this".
    """
    import oncallbot.diagnose as dg

    seen: dict[str, object] = {"kinds": []}

    def fake(cfg, system, prompt):
        if system is dg.FOLLOWUP_PLAN_SYSTEM_PROMPT:
            seen["kinds"].append("plan")  # type: ignore[union-attr]
            seen["plan_prompt"] = prompt
            return iter([json.dumps(plan or {"question": "", "calls": []})])
        kind = (
            "reason" if system is dg.REASON_SYSTEM_PROMPT
            else "findings" if system is dg.FINDINGS_SYSTEM_PROMPT
            else "followup" if system is dg.FOLLOWUP_SYSTEM_PROMPT
            else "other"
        )
        seen["kinds"].append(kind)  # type: ignore[union-attr]
        if kind != "followup":
            seen.setdefault("kind", kind)
            assert seen["kind"] == expect, f"expected {expect}, got {kind}"
        return iter([followup if kind == "followup" else text])

    monkeypatch.setattr(dg, "_stream_reason", fake)
    return seen


def test_thread_diagnosis_runs_checks_then_findings(monkeypatch):
    """All checks pass, so the open analysis runs and the reason does not."""
    import oncallbot.diagnose as dg

    monkeypatch.setattr(
        dg, "read_thread_state",
        lambda cfg, t: dg.ThreadState(closed=False, reason="nobody has replied"),
    )
    seen = _fake_prose(monkeypatch, "The `patient_id` was reused.", expect="findings")

    thread = _thread()
    thread.subject = f"Trends Error||{OGID}"
    d = dg.diagnose_thread(_cfg(), thread, client=StubClient(), order_group_id=OGID)

    assert d.verdict == MATCH
    assert seen["kind"] == "findings", "a passing check must not be narrated as a reason"
    assert d.other_findings == "The `patient_id` was reused."
    assert d.reason == ""
    assert d.thread_closed is False
    assert d.closure_reason == "nobody has replied"


def test_thread_diagnosis_writes_a_reason_when_a_check_fails(monkeypatch):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    seen = _fake_prose(monkeypatch, "the honorific blocks it", expect="reason")

    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = dg.diagnose_thread(_cfg(), _thread(), client=c, order_group_id=OGID)
    assert d.verdict == MISMATCH
    assert seen["kind"] == "reason", "findings must not run when a check failed"
    assert d.reason == "the honorific blocks it"
    assert d.other_findings == ""


def test_thread_diagnosis_finds_the_order_id_in_the_mail(monkeypatch):
    import oncallbot.diagnose as dg
    from datetime import datetime, timezone

    from oncallbot.models import EmailMessage, EmailThread

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    _fake_prose(monkeypatch, "", expect="findings")

    m = EmailMessage(
        id="m1", thread_id="t1", date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        sender="a@b.com", to="", cc="", subject=f"Trends Error||{OGID}||x@y.com",
        body_text="broken", snippet="",
    )
    d = dg.diagnose_thread(
        _cfg(), EmailThread(id="t1", subject=m.subject, messages=[m]), client=StubClient()
    )
    assert d.order_group_id == OGID
    assert d.thread_subject.startswith("Trends Error")


def test_thread_diagnosis_asks_for_an_order_id_when_there_is_none(monkeypatch):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(
        dg, "read_thread_state",
        lambda cfg, t: dg.ThreadState(closed=False, reason="unanswered complaint"),
    )
    d = dg.diagnose_thread(_cfg(), _thread(), client=StubClient())

    assert d.verdict == INDETERMINATE
    assert "No order group id" in d.blocked_because
    assert d.checks == []
    assert d.thread_closed is False


def test_closure_defaults_to_open_when_the_model_fails(monkeypatch):
    """Marking a live ticket closed is the expensive mistake."""
    import oncallbot.diagnose as dg

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(dg, "_complete_json", boom)
    st = dg.read_thread_state(_cfg(), _thread())
    assert st.closed is False


def _closure(monkeypatch, payload):
    import json as _json

    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "_complete_json", lambda cfg, s, p: _json.dumps(payload))
    return dg.read_thread_state(_cfg(), _thread())


def test_a_confident_resolving_reply_closes_the_thread(monkeypatch):
    st = _closure(monkeypatch, {
        "closed": True, "confidence": 0.93,
        "closed_by": "Support <care@1mg.com>", "closed_at": "2026-08-15 12:00",
        "reason": "agent attached the corrected report",
    })
    assert st.closed is True
    assert st.confidence == 0.93
    assert st.closed_by == "Support <care@1mg.com>"
    assert st.closed_at == "2026-08-15 12:00"


def test_a_low_confidence_closure_stays_open_and_says_why(monkeypatch):
    """The threshold is applied in code, not left to the model."""
    from oncallbot.diagnose import CLOSURE_CONFIDENCE_THRESHOLD

    st = _closure(monkeypatch, {
        "closed": True, "confidence": CLOSURE_CONFIDENCE_THRESHOLD - 0.2,
        "closed_by": "Support", "reason": "a reply mentions a fix",
    })
    assert st.closed is False
    assert "not clearly enough" in st.reason
    assert st.closed_by == "", "no closer is claimed on an open thread"


@pytest.mark.parametrize("bad", ["high", None, {}, -1, 5])
def test_an_unusable_confidence_is_treated_as_zero(monkeypatch, bad):
    st = _closure(monkeypatch, {"closed": True, "confidence": bad})
    assert st.closed is False or st.confidence == 1.0


def test_an_open_thread_reports_no_closer(monkeypatch):
    st = _closure(monkeypatch, {
        "closed": False, "confidence": 0.9,
        "closed_by": "someone", "reason": "the last message is a chase-up",
    })
    assert st.closed is False
    assert st.closed_by == ""
    assert st.reason == "the last message is a chase-up"


def test_closure_prompt_requires_a_resolving_reply():
    from oncallbot.diagnose import CLOSURE_SYSTEM_PROMPT

    flat = " ".join(CLOSURE_SYSTEM_PROMPT.split())
    assert "REPLIED IN THIS THREAD" in flat
    assert "is NOT closure" in flat
    assert "confidence" in flat


def test_the_closure_verdict_is_independent_of_the_checks(monkeypatch):
    """A ticket can be closed with a failed check, or open with all passing."""
    import oncallbot.diagnose as dg

    monkeypatch.setattr(
        dg, "read_thread_state",
        lambda cfg, t: dg.ThreadState(closed=True, confidence=0.95, closed_by="Support"),
    )
    _fake_prose(monkeypatch, "the honorific blocks it", expect="reason")

    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = dg.diagnose_thread(_cfg(), _thread(), client=c, order_group_id=OGID)

    assert d.verdict == MISMATCH, "a check still failed"
    assert d.thread_closed is True, "and the thread is still closed"


def test_thread_is_fenced_and_redacted_before_the_model_sees_it():
    from oncallbot.diagnose import _thread_for_model

    out = _thread_for_model(_thread())
    assert "BEGIN UNTRUSTED EMAIL THREAD" in out
    assert "END UNTRUSTED EMAIL THREAD" in out
    assert "9876543210" not in out, "phone numbers are masked"


def test_findings_prompt_forbids_re_arguing_a_settled_check():
    from oncallbot.diagnose import FINDINGS_SYSTEM_PROMPT

    flat = " ".join(FINDINGS_SYSTEM_PROMPT.split())
    assert "already been settled in code" in flat
    assert "Never re-argue a check" in flat
    assert "backticks" in flat
    assert "no digitisation record" in flat


# --- multiple bookings: the newest is not always the digitised one ----------


def test_chain_tries_every_booking_until_one_has_a_report():
    """The regression: picking the newest booking and giving up reported a
    failure that was not there, on an order whose other bookings were fine."""
    tried: list[str] = []

    class MultiBooking(StubClient):
        def orders(self, user_id, ogid=None):
            self.calls.append("orders")
            return [
                {"order_group_id": OGID, "booking_id": "b-newest", "patient_id": "p-1",
                 "delivery_time": "2026-07-30 16:57:19",
                 "test_name": "Good Health Gold Package"},
                {"order_group_id": OGID, "booking_id": "b-older", "patient_id": "p-1",
                 "delivery_time": "2026-07-30 13:30:46", "test_name": "ESR"},
            ]

        def json_report_url(self, booking_id, ogid):
            tried.append(booking_id)
            if booking_id == "b-newest":
                raise HraError("No booking details found for the given filters")
            return {"json_url": "https://s3/r.json"}

    d = diagnose_order(_cfg(), OGID, client=MultiBooking())

    assert tried == ["b-newest", "b-older"], "newest first, then fall through"
    assert d.verdict == MATCH
    assert d.booking_id == "b-older", "checks ran against the booking that had a report"
    assert [b["booking_id"] for b in d.bookings_without_report] == ["b-newest"]
    assert d.bookings_without_report[0]["test_name"] == "Good Health Gold Package"
    assert d.booking_count == 2


def test_a_missing_digitisation_record_is_reported_as_a_finding():
    class NoReports(StubClient):
        def orders(self, user_id, ogid=None):
            return [
                {"order_group_id": OGID, "booking_id": "b-1", "patient_id": "p-1",
                 "test_name": "Gold Package", "status": "delivered"},
            ]

        def json_report_url(self, booking_id, ogid):
            raise HraError("No booking details found for the given filters")

    d = diagnose_order(_cfg(), OGID, client=NoReports())
    assert d.verdict == INDETERMINATE
    assert "digitisation record" in d.blocked_because
    assert "b-1" in d.blocked_because
    assert [b["booking_id"] for b in d.bookings_without_report] == ["b-1"]


def test_findings_still_run_when_no_report_exists(monkeypatch):
    """The checks cannot run, but why they cannot is itself worth reporting."""
    import oncallbot.diagnose as dg

    class NoReports(StubClient):
        def orders(self, user_id, ogid=None):
            return [{"order_group_id": OGID, "booking_id": "b-1", "patient_id": "p-1",
                     "test_name": "Gold Package"}]

        def json_report_url(self, booking_id, ogid):
            raise HraError("No booking details found")

    monkeypatch.setattr(
        dg, "read_thread_state",
        lambda cfg, t: dg.ThreadState(closed=False, reason="smart report still missing"),
    )
    _fake_prose(monkeypatch, "`b-1` has no digitisation record.", expect="findings")
    d = dg.diagnose_thread(_cfg(), _thread(), client=NoReports(), order_group_id=OGID)

    assert d.verdict == INDETERMINATE
    assert d.other_findings == "`b-1` has no digitisation record."


def test_findings_prompt_carries_the_missing_records(monkeypatch):
    from oncallbot.diagnose import Diagnosis, find_other_findings
    import oncallbot.diagnose as dg

    captured = {}
    # Via monkeypatch, not a bare assignment: a bare one leaks into every test
    # that runs after this file and silently disables the real call.
    monkeypatch.setattr(
        dg, "_stream_reason",
        lambda cfg, sysp, prompt: (captured.update(p=prompt) or [""]),
    )

    d = Diagnosis(
        order_group_id=OGID, verdict=INDETERMINATE,
        bookings_without_report=[
            {"booking_id": "b-1", "test_name": "Gold Package",
             "status": "delivered", "delivery_time": "2026-07-30"}
        ],
    )
    find_other_findings(_cfg(), _thread(), d)
    assert "NO digitisation record" in captured["p"]
    assert "Gold Package" in captured["p"]


# --- the prose is a bullet list ---------------------------------------------


def test_both_prose_prompts_ask_for_a_bullet_list():
    """The UI renders bullets, so a paragraph would render as one long line."""
    import oncallbot.diagnose as dg

    for prompt in (dg.REASON_SYSTEM_PROMPT, dg.FINDINGS_SYSTEM_PROMPT):
        assert 'starting with "- "' in prompt
        assert "No nested bullets" in prompt
        assert "under about 30 words" in prompt, "long bullets are not pointers"
        assert "Never use\n  italics" in prompt, "italics would render as punctuation"
        assert "**bold**" in prompt
        # Nothing around the list: a preamble would render as a stray line.
        assert "No preamble" in prompt


def test_the_reason_prompt_still_refuses_to_relitigate_the_verdict():
    """Formatting changed; what the model is allowed to say did not."""
    from oncallbot.diagnose import REASON_SYSTEM_PROMPT as p

    assert "The verdict is settled" in p
    assert "Never invent an id" in p
    assert "backticks" in p


def test_the_findings_prompt_keeps_its_ordering_rule():
    from oncallbot.diagnose import FINDINGS_SYSTEM_PROMPT as p

    assert "Most likely cause first" in p
    assert "untrusted" in p


def test_terminal_output_drops_bold_as_well_as_backticks():
    """rich renders neither, so the marks would show up as punctuation."""
    from oncallbot.diagnose import strip_code_marks

    assert strip_code_marks("- `PO1` was **rewritten**.") == "- PO1 was rewritten."
    # Models reach for italics even when told not to.
    assert strip_code_marks("- the *current* record") == "- the current record"
    assert strip_code_marks("- 3 * 4 stays") == "- 3 * 4 stays"


def test_a_bullet_block_is_indented_line_by_line_for_the_terminal():
    """A single leading indent would align only the first bullet."""
    from oncallbot.cli import _block

    out = _block("- One **bold** point with `PO1`.\n- Two points.\n")
    assert out.splitlines() == ["  - One bold point with PO1.", "  - Two points."]


def test_a_block_escapes_rich_markup_in_model_text():
    """A [bold] in model text must print, not restyle the terminal."""
    from oncallbot.cli import _block

    assert _block("- Value was [bold] then gone.") == "  - Value was \\[bold] then gone."


# --- answering the further check ------------------------------------------


def _d(**kw):
    base = dict(order_group_id=OGID, verdict=MATCH, user_id="u-1",
                patient_id="p-1", booking_id="b-1")
    base.update(kw)
    from oncallbot.diagnose import Diagnosis

    return Diagnosis(**base)


def _plan(monkeypatch, payload):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "_complete_json", lambda cfg, s, p: json.dumps(payload))


def test_established_ids_are_the_only_ones_offered(monkeypatch):
    from oncallbot.diagnose import established_ids

    d = _d(booking_id="")
    assert established_ids(d) == {
        "order_group_id": OGID, "user_id": "u-1", "patient_id": "p-1"
    }


def test_the_further_check_becomes_a_read_call(monkeypatch):
    """The example case: 'pull the full booking history for this patient'."""
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "question": "pull the full booking history for the user",
        "calls": [{"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}}],
    })
    question, calls = plan_followup_reads(
        _cfg(), _d(), "- Further check: pull the full booking history."
    )
    assert question == "pull the full booking history for the user"
    assert calls == [{"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}}]
    # Unfiltered on purpose: passing order_group_id would return the one order
    # the diagnosis already has.
    assert "order_group_id" not in calls[0]["params"]


def test_an_invented_id_is_refused(monkeypatch):
    """The safety property: only ids this diagnosis established are queryable."""
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "question": "check the other patient",
        "calls": [{"tool": "get_patient_versions_audit_trail",
                   "params": {"patient_id": "657ec540-09c5-4edf-b00f-30a6d2cacee9"}}],
    })
    _question, calls = plan_followup_reads(_cfg(), _d(), "- Further check: that other profile.")
    assert calls == [], "an id we never established must not be queried"


def test_an_id_lifted_out_of_the_email_is_refused(monkeypatch):
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "calls": [{"tool": "get_user_details_for_an_order",
                   "params": {"order_group_id": "PO99999999999-999"}}],
    })
    _q, calls = plan_followup_reads(_cfg(), _d(), "- Further check: the order in the mail.")
    assert calls == []


def test_a_booking_without_a_report_counts_as_established(monkeypatch):
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "calls": [{"tool": "get_json_report_url_of_a_booking",
                   "params": {"booking_id": "b-2", "order_group_id": OGID}}],
    })
    d = _d(bookings_without_report=[{"booking_id": "b-2", "test_name": "Gold"}])
    _q, calls = plan_followup_reads(_cfg(), d, "- Further check: the package booking.")
    assert calls[0]["params"]["booking_id"] == "b-2"


def test_a_missing_id_is_filled_from_the_chain(monkeypatch):
    """The chain already resolved these, so asking again is not the answer."""
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "calls": [{"tool": "fetch_all_orders_of_a_user", "params": {}}],
    })
    _q, calls = plan_followup_reads(_cfg(), _d(), "- Further check: bookings.")
    assert calls == [{"tool": "fetch_all_orders_of_a_user",
                      "params": {"user_id": "u-1"}}]


def test_bookings_parameters_is_never_called_without_its_ids(monkeypatch):
    """It needs order + patient + booking, and those come from calls 1 and 2.

    Called with the order alone it answers for the whole order, and the reply
    then reads as if the booking's data were missing rather than never asked
    for -- so an incompletable call is dropped instead.
    """
    from oncallbot.diagnose import plan_followup_reads

    _plan(monkeypatch, {
        "calls": [{"tool": "get_diagnostic_bookings_parameters",
                   "params": {"order_group_id": OGID}}],
    })

    # Everything known: patient and booking are filled in from the chain.
    _q, calls = plan_followup_reads(_cfg(), _d(), "- Further check: parameters.")
    assert calls[0]["params"] == {
        "order_group_id": OGID, "patient_id": "p-1", "booking_id": "b-1",
    }

    # No booking established, so there is nothing honest to fill it with.
    _q, calls = plan_followup_reads(
        _cfg(), _d(booking_id=""), "- Further check: parameters."
    )
    assert calls == []


def test_the_executor_refuses_bookings_parameters_without_its_ids():
    """Belt and braces: the last check does not depend on the planner."""
    from oncallbot.tools.executor import ToolError, call_tool
    from oncallbot.tools.registry import read_only_tools

    spec = next(
        t for t in read_only_tools() if t.name == "get_diagnostic_bookings_parameters"
    )
    with pytest.raises(ToolError) as exc:
        call_tool(object(), spec, {"order_group_id": OGID})
    assert "patient_id, booking_id" in str(exc.value)
    # And it says where they come from.
    assert "get_user_details_for_an_order then fetch_all_orders_of_a_user" in str(exc.value)


def test_the_url_signer_is_never_offered(monkeypatch):
    import oncallbot.diagnose as dg

    seen = {}
    monkeypatch.setattr(
        dg, "_complete_json",
        lambda cfg, s, p: seen.update(prompt=p) or json.dumps({"calls": []}),
    )
    dg.plan_followup_reads(_cfg(), _d(), "- Further check: open the pdf.")
    assert "get_presigned_url_for_private_content" not in seen["prompt"]
    assert "fetch_all_orders_of_a_user" in seen["prompt"]
    # It must be told which ids it may use, and that it already has the rest.
    assert "u-1" in seen["prompt"] and "already established" in seen["prompt"]


def test_no_plan_when_the_findings_are_empty(monkeypatch):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(
        dg, "_complete_json", lambda *a: pytest.fail("nothing to plan from")
    )
    assert dg.plan_followup_reads(_cfg(), _d(), "  ") == ("", [])


def test_calls_are_capped(monkeypatch):
    import oncallbot.diagnose as dg

    _plan(monkeypatch, {"calls": [
        {"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}},
        {"tool": "get_patient_versions_audit_trail", "params": {"patient_id": "p-1"}},
        {"tool": "get_user_details_for_an_order", "params": {"order_group_id": OGID}},
    ]})
    _q, calls = dg.plan_followup_reads(_cfg(), _d(), "- Further check: everything.")
    assert len(calls) == dg.MAX_FOLLOWUP_CALLS


def test_running_a_read_reports_what_came_back(monkeypatch):
    from oncallbot.diagnose import run_followup_reads

    class C:
        def get(self, path, query=None):
            assert path.endswith("/user/u-1/orders"), path
            assert not query, "unfiltered: the whole history, not this order"
            return [{"booking_id": "b-1"}, {"booking_id": "b-9"}]

    tried, results = run_followup_reads(
        C(), [{"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}}]
    )
    assert tried == [{"tool": "fetch_all_orders_of_a_user", "params": "user_id=u-1",
                      "outcome": "returned 2 item(s)"}]
    assert len(results["fetch_all_orders_of_a_user"]) == 2


def test_an_empty_result_is_reported_as_such():
    from oncallbot.diagnose import run_followup_reads

    class C:
        def get(self, path, query=None):
            return []

    tried, results = run_followup_reads(
        C(), [{"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}}]
    )
    assert tried[0]["outcome"] == "returned nothing"
    # The empty list is kept: "ran and found nothing" is not "never ran".
    assert results == {"fetch_all_orders_of_a_user": []}


def test_a_failed_read_does_not_end_the_diagnosis():
    from oncallbot.diagnose import run_followup_reads

    class C:
        def get(self, path, query=None):
            raise HraError("502 from the gateway")

    tried, results = run_followup_reads(
        C(), [{"tool": "fetch_all_orders_of_a_user", "params": {"user_id": "u-1"}}]
    )
    assert "failed: 502 from the gateway" in tried[0]["outcome"]
    assert results == {}


def test_the_followup_prompt_carries_the_calls_and_the_data():
    from oncallbot.diagnose import build_followup_prompt

    prompt = build_followup_prompt(
        "pull the full booking history",
        [{"tool": "fetch_all_orders_of_a_user", "params": "user_id=u-1",
          "outcome": "returned 2 item(s)"}],
        {"fetch_all_orders_of_a_user": [{"booking_id": "b-9"}]},
    )
    assert "Further check: pull the full booking history" in prompt
    assert "fetch_all_orders_of_a_user(user_id=u-1) -> returned 2 item(s)" in prompt
    assert "b-9" in prompt


def test_the_followup_prompt_says_nothing_came_back():
    from oncallbot.diagnose import build_followup_prompt

    prompt = build_followup_prompt("check it", [], {})
    assert "(nothing)" in prompt


def test_the_followup_prompt_forbids_filling_gaps():
    from oncallbot.diagnose import FOLLOWUP_SYSTEM_PROMPT as p

    assert "empty\n  result is a real answer" in p
    assert "never fill a gap with what you would expect" in p.replace("\n  ", " ")
    assert "FIRST bullet must say whether the data settles the check" in p


def test_the_whole_chain_ends_with_the_further_check_answered(monkeypatch):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    seen = _fake_prose(
        monkeypatch,
        "- Further check: pull the full booking history for `u-1`.",
        expect="findings",
        plan={"question": "pull the full booking history for `u-1`",
              "calls": [{"tool": "fetch_all_orders_of_a_user",
                         "params": {"user_id": "u-1"}}]},
        followup="- **No** prior booking has a digitised report.",
    )

    class C(StubClient):
        def get(self, path, query=None):
            return [{"booking_id": "b-7", "test_name": "CBC"}]

    d = dg.diagnose_thread(_cfg(), _thread(), client=C(), order_group_id=OGID)

    assert d.verdict == MATCH
    assert seen["kinds"] == ["findings", "plan", "followup"], "in that order"
    assert d.followup_question == "pull the full booking history for `u-1`"
    assert d.followup_calls[0]["outcome"] == "returned 1 item(s)"
    assert d.followup_findings == "- **No** prior booking has a digitised report."


def test_the_chain_says_so_when_no_read_can_answer(monkeypatch):
    """Most further checks need a human. That has to be said, not implied."""
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    seen = _fake_prose(
        monkeypatch, "- Further check: ask the lab to re-upload.",
        plan={"question": "ask the lab to re-upload the report", "calls": []},
    )
    d = dg.diagnose_thread(_cfg(), _thread(), client=StubClient(), order_group_id=OGID)

    assert seen["kinds"] == ["findings", "plan"], "no prose without data"
    assert d.followup_findings == ""
    assert d.followup_calls[0]["outcome"] == "no documented read-only API can answer this"


def test_a_mismatch_does_not_chase_a_further_check(monkeypatch):
    """A fired runbook has a resolution, not an open question."""
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    seen = _fake_prose(monkeypatch, "- The honorific blocks it.", expect="reason")
    c = StubClient(report={"PatientName": "Mr. Ravi Kumar", "Gender": "male"})
    d = dg.diagnose_thread(_cfg(), _thread(), client=c, order_group_id=OGID)

    assert d.verdict == MISMATCH
    assert seen["kinds"] == ["reason"]
    assert d.followup_calls == []


def test_each_call_gets_its_own_share_of_the_prompt_budget():
    """The bug: one fat payload ate the budget and the other call's data
    vanished, so the model reported it as "not provided"."""
    from oncallbot.diagnose import build_followup_prompt

    fat = {"parameters": [{"name": f"param-{i}", "value": i} for i in range(2000)]}
    prompt = build_followup_prompt(
        "check both",
        [],
        {"get_diagnostic_bookings_parameters": fat,
         "get_user_details_for_an_order": {"user_id": "u-1", "patient_details": []}},
    )
    assert "--- get_diagnostic_bookings_parameters ---" in prompt
    assert "--- get_user_details_for_an_order ---" in prompt
    assert "u-1" in prompt, "the second call's data must survive the truncation"
    # And the cut is stated, so missing data is not read as absent data.
    assert "cut off here" in prompt
    assert "Do not treat what is missing as absent" in prompt


def test_the_running_placeholder_names_the_parameters(monkeypatch):
    import oncallbot.diagnose as dg

    monkeypatch.setattr(dg, "read_thread_state", lambda cfg, t: dg.ThreadState())
    _fake_prose(
        monkeypatch, "- Further check: history.",
        plan={"question": "q", "calls": [{"tool": "fetch_all_orders_of_a_user",
                                          "params": {"user_id": "u-1"}}]},
        followup="- Nothing there.",
    )

    class C(StubClient):
        def get(self, path, query=None):
            return [{"booking_id": "b-7"}]

    events = list(dg.diagnose_thread_stream(
        _cfg(), _thread(), client=C(), order_group_id=OGID
    ))
    running = [p for k, p in events if k == "followup"][0]
    assert running["calls"][0]["params"] == "user_id=u-1"
    assert running["calls"][0]["outcome"] == "running"


def test_the_further_check_signs_urls_before_the_model_sees_them():
    """The findings quote what they are given, and a raw private URL 403s."""
    import oncallbot.diagnose as dg

    class C:
        def post_read(self, path, body):
            return {"url": body["url"] + "?X-Amz-Signature=abc"}

    results = {
        "get_json_report_url_of_a_booking": {
            "json_url": "https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/d/x.json"
        }
    }
    dg._sign_urls_in_place(C(), results)

    url = results["get_json_report_url_of_a_booking"]["json_url"]
    assert url.endswith("?X-Amz-Signature=abc")
    assert "x.json?X-Amz-Signature" in dg.build_followup_prompt("q", [], results)


def test_a_signer_failure_leaves_the_diagnosis_alone():
    import oncallbot.diagnose as dg

    class C:
        def post_read(self, path, body):
            raise RuntimeError("signer down")

    results = {"x": {"json_url": "https://ok/a.json"}}
    dg._sign_urls_in_place(C(), results)
    assert results == {"x": {"json_url": "https://ok/a.json"}}, "unchanged, not lost"
