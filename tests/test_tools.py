"""The order_info.md tool registry, its guards, and the order Q&A loop."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from oncallbot.config import Config, HraConfig
from oncallbot.hra_client import HraClient, HraError
from oncallbot.order_qa import Fetched, find_order_ids, gather_entry, plan_followups
from oncallbot.tools.executor import ToolError, call_tool
from oncallbot.tools.registry import (
    ToolSpec,
    load_tools,
    read_only_tools,
    withheld_tools,
)

DOC = Path("src/oncallbot/tools/order_info.md")


# --- parsing the reference -------------------------------------------------


def test_every_documented_endpoint_is_parsed():
    tools = load_tools(DOC)
    assert len(tools) >= 6
    numbers = [t.number for t in tools]
    assert numbers == sorted(numbers), "numbering order is preserved"


def test_all_documented_endpoints_are_currently_reads():
    """The file was trimmed to reads; withheld should be empty."""
    assert withheld_tools(DOC) == []
    assert len(read_only_tools(DOC)) == len(load_tools(DOC))


def test_specs_carry_paths_params_and_descriptions():
    by_name = {t.name: t for t in read_only_tools(DOC)}
    users = by_name["get_user_details_for_an_order"]
    assert users.method == "GET"
    assert users.path == "/users/order/{}"
    assert users.path_params == ["order_group_id"]
    assert "user_id" in users.description

    orders = by_name["fetch_all_orders_of_a_user"]
    assert orders.path_params == ["user_id"]
    assert orders.query_params == ["order_group_id"]


def test_presigned_url_is_the_only_post_treated_as_a_read():
    posts = [t for t in load_tools(DOC) if t.method == "POST"]
    assert [t.path for t in posts] == ["/presigned-url"]
    assert all(t.is_read for t in posts)


def test_a_documented_write_would_be_withheld():
    """Guard the classifier itself, since the live file has no writes now."""
    write = ToolSpec(
        number=9, title="Update booking", description="moves an order",
        method="PUT", url_template="", path="/user/{}/booking",
        path_params=["user_id"],
    )
    assert write.is_read is False


def test_missing_reference_file_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="API reference"):
        load_tools(tmp_path / "nope.md")


def test_describe_lists_what_a_tool_needs():
    spec = {t.name: t for t in read_only_tools(DOC)}["get_patient_versions_audit_trail"]
    text = spec.describe()
    assert "get_patient_versions_audit_trail" in text
    assert "patient_id" in text


# --- executor guards -------------------------------------------------------


def _client(handler, monkeypatch):
    cfg = HraConfig(token_env="T")
    monkeypatch.setenv("T", "tok")
    c = HraClient(cfg)
    c.client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=cfg.base_url + cfg.path_prefix
    )
    return c


def test_executor_refuses_a_write_even_if_handed_one(monkeypatch):
    """Second, independent check: the registry filters and this refuses again."""
    write = ToolSpec(
        number=7, title="Update patient info", description="", method="PUT",
        url_template="", path="/patient/{}/patient-info", path_params=["patient_id"],
    )
    called = []
    c = _client(lambda r: called.append(r) or httpx.Response(200, json={}), monkeypatch)
    with pytest.raises(ToolError, match="not callable here"):
        call_tool(c, write, {"patient_id": "p-1"})
    assert called == [], "nothing may reach the network"


def test_executor_builds_path_and_query(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"orders": []}})

    spec = {t.name: t for t in read_only_tools(DOC)}["fetch_all_orders_of_a_user"]
    call_tool(
        _client(handler, monkeypatch),
        spec,
        {"user_id": "u-1", "order_group_id": "PO1-11"},
    )
    assert seen["path"].endswith("/user/u-1/orders")
    assert seen["query"] == {"order_group_id": "PO1-11"}


def test_executor_requires_path_params(monkeypatch):
    spec = {t.name: t for t in read_only_tools(DOC)}["get_patient_versions_audit_trail"]
    with pytest.raises(ToolError, match="patient_id"):
        call_tool(_client(lambda r: httpx.Response(200), monkeypatch), spec, {})


def test_post_read_refuses_any_path_but_presigned_url(monkeypatch):
    c = _client(lambda r: httpx.Response(200, json={}), monkeypatch)
    with pytest.raises(HraError, match="refuses"):
        c.post_read("/patient/p-1/patient-info", {"name": "x"})


def test_presigned_url_call_needs_a_url(monkeypatch):
    spec = {t.name: t for t in read_only_tools(DOC)}["get_presigned_url_for_private_content"]
    with pytest.raises(ToolError, match="url"):
        call_tool(_client(lambda r: httpx.Response(200), monkeypatch), spec, {})


# --- order id extraction ---------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("who is on PO10003583002-668?", ["PO10003583002-668"]),
        ("booking pb10006149945-461 please", ["PB10006149945-461"]),
        ("PO10003583002-668 and PB10006149945-461", ["PO10003583002-668", "PB10006149945-461"]),
        ("no ids here", []),
        ("order 18240039", []),          # droplet id, not an order group
        ("PO123", []),                    # too short to be real
    ],
)
def test_find_order_ids(text, expected):
    assert find_order_ids(text) == expected


def test_find_order_ids_searches_history_too():
    got = find_order_ids("and its versions?", "earlier I asked about PO10003583002-668")
    assert got == ["PO10003583002-668"]


def test_find_order_ids_deduplicates():
    assert find_order_ids("PO10003583002-668 PO10003583002-668") == ["PO10003583002-668"]


# --- the loop --------------------------------------------------------------


def _entry_handler(request):
    p = request.url.path
    if "/users/order/" in p:
        return httpx.Response(200, json={"data": {
            "user_id": "u-1",
            "patient_details": [{"id": "p-1", "name": "Ujjwal Das", "gender": "m"}]}})
    if p.endswith("/orders"):
        return httpx.Response(200, json={"data": {"orders": [
            {"id": "PB1-1", "patient_id": "p-1", "order_group_id": "PO1-11",
             "test_name": "CBC", "status": "delivered"}]}})
    return httpx.Response(404, json={"error": p})


def test_gather_entry_makes_both_calls_and_records_ids(monkeypatch):
    f = gather_entry(_client(_entry_handler, monkeypatch), "PO1-11")
    assert f.trail == ["get_user_details_for_an_order", "fetch_all_orders_of_a_user"]
    assert f.user_id == "u-1"
    assert [p["id"] for p in f.patients] == ["p-1"]
    assert [b["id"] for b in f.bookings] == ["PB1-1"]
    assert f.known_ids()["booking_ids"] == ["PB1-1"]


def test_gather_entry_skips_orders_when_there_is_no_user(monkeypatch):
    def handler(request):
        if "/users/order/" in request.url.path:
            return httpx.Response(200, json={"data": {"patient_details": []}})
        raise AssertionError("orders must not be called without a user_id")

    f = gather_entry(_client(handler, monkeypatch), "PO1-11")
    assert any("No user_id" in e for e in f.errors)


def test_plan_drops_placeholder_params(monkeypatch):
    """A '<patient_id>' means the model lacked the id; never send it upstream."""
    import oncallbot.order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, sys_, prompt: json.dumps({"calls": [
            {"tool": "get_patient_versions_audit_trail", "params": {"patient_id": "<patient_id>"}},
            {"tool": "get_patient_versions_audit_trail", "params": {"patient_id": "p-1"}},
        ]}),
    )
    f = Fetched(order_group_id="PO1-11", user_id="u-1")
    calls = plan_followups(Config(), "q", f)
    assert calls == [{"tool": "get_patient_versions_audit_trail", "params": {"patient_id": "p-1"}}]


def test_plan_drops_unknown_tool_names(monkeypatch):
    import oncallbot.order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, sys_, prompt: json.dumps({"calls": [
            {"tool": "update_patient_info", "params": {"patient_id": "p-1"}},
            {"tool": "delete_everything", "params": {}},
        ]}),
    )
    assert plan_followups(Config(), "q", Fetched(order_group_id="PO1-11")) == []


def test_plan_caps_the_number_of_calls(monkeypatch):
    import oncallbot.order_qa as qa

    many = [{"tool": "get_patient_versions_audit_trail", "params": {"patient_id": f"p-{i}"}}
            for i in range(10)]
    monkeypatch.setattr(qa, "_complete_json", lambda *a: json.dumps({"calls": many}))
    assert len(plan_followups(Config(), "q", Fetched(order_group_id="PO1-11"))) == qa.MAX_FOLLOWUPS


def test_plan_survives_unparseable_model_output(monkeypatch):
    import oncallbot.order_qa as qa

    monkeypatch.setattr(qa, "_complete_json", lambda *a: "not json at all")
    assert plan_followups(Config(), "q", Fetched(order_group_id="PO1-11")) == []


def test_plan_returns_nothing_when_everything_is_fetched(monkeypatch):
    f = Fetched(order_group_id="PO1-11")
    f.results = {t.name: {} for t in read_only_tools(DOC)}
    assert plan_followups(Config(), "q", f) == []


# --- intent routing --------------------------------------------------------


def test_intent_accepts_a_real_order_id():
    from oncallbot.chat.intent import Intent

    i = Intent.from_dict(
        {"action": "order_lookup", "order_group_id": "po10003583002-668"}, ["other"]
    )
    assert i.action == "order_lookup"
    assert i.order_group_id == "PO10003583002-668"


@pytest.mark.parametrize("bad", ["<order_group_id>", "PO123", "", "the order", None, 12345])
def test_intent_rejects_a_placeholder_or_malformed_order_id(bad):
    from oncallbot.chat.intent import Intent

    i = Intent.from_dict({"action": "order_lookup", "order_group_id": bad}, ["other"])
    assert i.order_group_id == ""


def test_router_prompt_describes_the_order_lookup_action():
    from datetime import date

    from oncallbot.chat.intent import system_prompt

    p = system_prompt(date(2026, 9, 10))
    assert "order_lookup" in p
    assert "PO" in p


# --- a booking question always gets the bookings API ------------------------


def _fetched(bookings=None, results=None):
    from oncallbot.order_qa import Fetched

    f = Fetched(order_group_id="PO10003583002-668")
    f.user_id = "u-1"
    f.bookings = bookings if bookings is not None else [
        {"booking_id": "PB10006826270-566", "patient_id": "p-1",
         "order_group_id": "PO10003583002-668", "delivery_time": "2026-09-02"},
        {"booking_id": "PB10006826271-777", "patient_id": "p-2",
         "order_group_id": "PO10003583002-668", "delivery_time": "2026-09-05"},
    ]
    f.results = results or {}
    return f


@pytest.mark.parametrize("q", [
    "what bookings are on this order",
    "which tests were ordered",
    "show me the digitised parameters",
    "what values did the lab return",
    "anything on PB10006826270-566?",
    "what are the results for this order",
])
def test_booking_questions_are_recognised(q):
    from oncallbot.order_qa import is_booking_question

    assert is_booking_question(q), q


@pytest.mark.parametrize("q", [
    "who is the patient on this order",
    "was the patient record edited",
    "what is the user id",
])
def test_other_questions_are_not(q):
    from oncallbot.order_qa import is_booking_question

    assert not is_booking_question(q)


def test_a_booking_id_in_earlier_chat_counts():
    from oncallbot.order_qa import is_booking_question

    assert is_booking_question("and that one?", "earlier: PB10006826270-566")


def test_every_booking_is_read_newest_first():
    from oncallbot.order_qa import BOOKINGS_TOOL, booking_calls

    calls = booking_calls(_fetched(), "what bookings are on this order")
    assert [c["tool"] for c in calls] == [BOOKINGS_TOOL] * 2
    assert [c["params"]["booking_id"] for c in calls] == [
        "PB10006826271-777", "PB10006826270-566"
    ]
    # order_group_id and patient_id come from the entry calls, not the question.
    assert calls[0]["params"]["order_group_id"] == "PO10003583002-668"
    assert calls[0]["params"]["patient_id"] == "p-2"


def test_a_named_booking_narrows_it_to_that_one():
    from oncallbot.order_qa import booking_calls

    calls = booking_calls(_fetched(), "what is on PB10006826270-566?")
    assert len(calls) == 1
    assert calls[0]["params"]["booking_id"] == "PB10006826270-566"


def test_a_booking_id_that_is_not_on_the_order_falls_back_to_all():
    """Better to show the order's real bookings than to call nothing."""
    from oncallbot.order_qa import booking_calls

    calls = booking_calls(_fetched(), "what about PB99999999999-999?")
    assert len(calls) == 2


def test_booking_calls_are_capped():
    from oncallbot.order_qa import MAX_BOOKING_CALLS, booking_calls

    many = [
        {"booking_id": f"PB1000000000{i}-100", "patient_id": "p-1",
         "delivery_time": f"2026-09-0{i}"}
        for i in range(1, 7)
    ]
    assert len(booking_calls(_fetched(many), "which tests")) == MAX_BOOKING_CALLS


def test_each_booking_keeps_its_own_result():
    """One shared key kept only the last booking's parameters."""
    from oncallbot.order_qa import booking_calls, run_followups

    class C:
        def get(self, path, query=None):
            return {"path": path, "query": dict(query or {})}

    f = _fetched()
    calls = booking_calls(f, "which tests")
    run_followups(C(), f, calls)

    assert sorted(f.results) == [
        "get_diagnostic_bookings_parameters[PB10006826270-566]",
        "get_diagnostic_bookings_parameters[PB10006826271-777]",
    ]
    one = f.results["get_diagnostic_bookings_parameters[PB10006826270-566]"]
    assert one["query"]["booking_id"] == "PB10006826270-566"
    assert one["query"]["patient_id"] == "p-1"
    assert "PO10003583002-668" in one["path"], "the order group id is in the path"


def test_the_planner_is_not_offered_a_tool_already_called_per_booking(monkeypatch):
    from oncallbot import order_qa as qa

    seen = {}
    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, s, p: seen.update(prompt=p) or json.dumps({"calls": []}),
    )
    f = _fetched(results={"get_diagnostic_bookings_parameters[PB1-1]": {}})
    qa.plan_followups(Config(), "which tests", f)
    prompt = seen["prompt"]
    offered = prompt[prompt.index("Available tools:"):prompt.index("Ids already known:")]
    assert "get_diagnostic_bookings_parameters" not in offered
    assert "get_patient_versions_audit_trail" in offered, "the rest still are"


def test_no_second_read_when_the_bookings_api_already_ran():
    from oncallbot.order_qa import booking_calls

    f = _fetched(results={"get_diagnostic_bookings_parameters": {}})
    assert booking_calls(f, "which tests") == []


def test_no_calls_when_the_order_has_no_bookings():
    from oncallbot.order_qa import booking_calls

    assert booking_calls(_fetched([]), "which tests") == []


def test_every_response_survives_the_prompt_budget():
    """The bug: one shared slice dropped the later bookings entirely, and the
    answer reported their calls as never made."""
    f = _fetched()
    fat = {"diagnostic_parameters": [{"name": f"p{i}", "value": i} for i in range(3000)]}
    f.results = {
        "get_diagnostic_bookings_parameters[PB1-1]": fat,
        "get_diagnostic_bookings_parameters[PB2-2]": fat,
        "get_user_details_for_an_order": {"user_id": "u-1"},
    }
    ctx = f.context_for_model(9000)

    for key in f.results:
        assert f"--- {key} ---" in ctx
    assert "u-1" in ctx, "the small response must not be crowded out"
    assert "cut off here" in ctx
    assert "Do not treat what is missing as absent" in ctx


def test_an_empty_context_says_nothing_rather_than_an_empty_object():
    assert Fetched(order_group_id="PO1-11").context_for_model() == "(nothing)"


# --- the dependency chain ---------------------------------------------------


def test_bookings_parameters_declares_its_prerequisites():
    """Its patient_id and booking_id do not exist until calls 1 and 2 return."""
    from oncallbot.tools.registry import PREREQUISITES, missing_prerequisites

    assert PREREQUISITES["get_diagnostic_bookings_parameters"] == (
        "get_user_details_for_an_order",
        "fetch_all_orders_of_a_user",
    )
    assert missing_prerequisites("get_diagnostic_bookings_parameters", []) == [
        "get_user_details_for_an_order", "fetch_all_orders_of_a_user"
    ]
    assert missing_prerequisites(
        "get_diagnostic_bookings_parameters",
        ["get_user_details_for_an_order", "fetch_all_orders_of_a_user"],
    ) == []
    # Halfway is still not enough.
    assert missing_prerequisites(
        "get_diagnostic_bookings_parameters", ["get_user_details_for_an_order"]
    ) == ["fetch_all_orders_of_a_user"]


def test_the_entry_calls_have_no_prerequisites():
    from oncallbot.tools.registry import missing_prerequisites

    assert missing_prerequisites("get_user_details_for_an_order", []) == []
    assert missing_prerequisites("fetch_all_orders_of_a_user", []) == []


def test_required_params_go_beyond_the_path():
    from oncallbot.tools.registry import missing_params, read_only_tools

    spec = next(
        t for t in read_only_tools() if t.name == "get_diagnostic_bookings_parameters"
    )
    assert spec.path_params == ["order_group_id"], "the path alone would allow it"
    assert missing_params(spec.name, {"order_group_id": "PO1-1"}, spec) == [
        "patient_id", "booking_id"
    ]
    assert missing_params(
        spec.name,
        {"order_group_id": "PO1-1", "patient_id": "p", "booking_id": "b"},
        spec,
    ) == []


def test_a_planned_call_is_refused_before_the_chain_has_run(monkeypatch):
    """The model cannot shortcut to call 3 on an order nothing was read for."""
    from oncallbot import order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, s, p: json.dumps({"calls": [
            {"tool": "get_diagnostic_bookings_parameters",
             "params": {"order_group_id": "PO1-11", "patient_id": "p-1",
                        "booking_id": "b-1"}},
        ]}),
    )
    # Nothing fetched yet, so neither entry call has run.
    empty = Fetched(order_group_id="PO1-11")
    assert plan_followups(Config(), "which tests", empty) == []


def test_a_planned_call_is_allowed_once_the_chain_has_run(monkeypatch):
    from oncallbot import order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, s, p: json.dumps({"calls": [
            {"tool": "get_diagnostic_bookings_parameters",
             "params": {"order_group_id": "PO10003583002-668",
                        "patient_id": "p-1", "booking_id": "PB10006826270-566"}},
        ]}),
    )
    f = _fetched(results={"get_user_details_for_an_order": {},
                          "fetch_all_orders_of_a_user": []})
    calls = plan_followups(Config(), "which tests", f)
    assert calls[0]["params"]["booking_id"] == "PB10006826270-566"


def test_a_booking_less_request_expands_over_every_booking(monkeypatch):
    """"Get me the parameters" without a booking means this order's bookings,
    not a call that answers for none of them."""
    from oncallbot import order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, s, p: json.dumps({"calls": [
            {"tool": "get_diagnostic_bookings_parameters", "params": {}},
        ]}),
    )
    f = _fetched(results={"get_user_details_for_an_order": {},
                          "fetch_all_orders_of_a_user": []})
    calls = plan_followups(Config(), "what parameters were digitised", f)

    assert [c["params"]["booking_id"] for c in calls] == [
        "PB10006826271-777", "PB10006826270-566"
    ]
    for c in calls:
        assert c["params"]["order_group_id"] == "PO10003583002-668"
        assert c["params"]["patient_id"]
        assert c["key"].startswith("get_diagnostic_bookings_parameters[")


def test_an_unrelated_tool_is_not_given_an_order_id(monkeypatch):
    """Only ids the tool declares, so the trail shows what was really sent."""
    from oncallbot import order_qa as qa

    monkeypatch.setattr(
        qa, "_complete_json",
        lambda cfg, s, p: json.dumps({"calls": [
            {"tool": "get_patient_versions_audit_trail", "params": {"patient_id": "p-1"}},
        ]}),
    )
    calls = plan_followups(Config(), "was it edited", _fetched())
    assert calls == [{"tool": "get_patient_versions_audit_trail",
                      "params": {"patient_id": "p-1"}}]


# --- signing the report files ----------------------------------------------


@pytest.mark.parametrize("q", [
    "can you give me a pre-signed url for this booking report so that I can access",
    "presigned url for the smart report",
    "can I open the report",
    "share the pdf with me",
    "give me a link to view the report",
    "how do I download it",
])
def test_report_link_questions_are_recognised(q):
    from oncallbot.order_qa import is_report_link_question

    assert is_report_link_question(q), q


@pytest.mark.parametrize("q", [
    "who is the patient on this order",
    "which tests were ordered",
    "was the patient record edited",
])
def test_other_questions_are_not_link_questions(q):
    from oncallbot.order_qa import is_report_link_question

    assert not is_report_link_question(q)


def _with_urls():
    f = _fetched()
    f.results = {
        "fetch_all_orders_of_a_user": [
            {"booking_id": "PB1-1",
             "report_url": "https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/upload/reports/1/a.pdf",
             "smart_pdf_url": "https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/upload/smart/b.pdf",
             "test_name": "CBC",
             "status": "delivered"},
        ],
        "get_json_report_url_of_a_booking": {
            "json_url": "https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/dump/c.json?sig=x"
        },
    }
    return f


def test_urls_are_taken_from_the_orders_own_data():
    from oncallbot.order_qa import find_private_urls

    urls = find_private_urls(_with_urls())
    assert [u.rsplit("/", 1)[-1] for u in urls] == ["a.pdf", "b.pdf", "c.json?sig=x"]


def test_a_url_in_the_question_is_never_signed():
    """The signer signs whatever it is handed, so the URL must come from the
    order's data -- otherwise a request could mint access to any object."""
    from oncallbot.order_qa import presign_calls

    calls = presign_calls(
        _fetched(),   # no results, so no URLs
        "sign https://1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/someone/else.pdf",
    )
    assert calls == []


def test_non_url_strings_and_non_url_keys_are_ignored():
    from oncallbot.order_qa import Fetched, find_private_urls

    f = Fetched(order_group_id="PO1-11")
    f.results = {
        "x": {
            "test_name": "https not a url",          # key is not a url key
            "note": "https://example.com/thing",     # ditto
            "report_url": "not-a-url",               # value is not a url
            "smart_pdf_url": "https://ok/a.pdf",
        }
    }
    assert find_private_urls(f) == ["https://ok/a.pdf"]


def test_the_same_file_is_not_signed_twice():
    from oncallbot.order_qa import Fetched, find_private_urls

    f = Fetched(order_group_id="PO1-11")
    f.results = {
        "a": {"report_url": "https://ok/a.pdf"},
        "b": {"report_url": "https://ok/a.pdf?already=signed"},
    }
    assert find_private_urls(f) == ["https://ok/a.pdf"]


def test_presign_calls_are_capped_and_ask_for_a_ttl():
    from oncallbot.order_qa import MAX_PRESIGN_CALLS, Fetched, presign_calls

    f = Fetched(order_group_id="PO1-11")
    f.results = {"x": {f"report_url_{i}": f"https://ok/{i}.pdf" for i in range(10)}}
    calls = presign_calls(f, "give me the links")
    assert len(calls) == MAX_PRESIGN_CALLS
    assert calls[0]["params"]["ttl"] == 3600
    # A distinct key each, so one result does not overwrite another.
    assert len({c["key"] for c in calls}) == len(calls)


def test_signing_goes_through_the_documented_post_read():
    from oncallbot.order_qa import presign_calls, run_followups

    seen = {}

    class C:
        def post_read(self, path, body):
            seen["path"] = path
            seen["body"] = body
            return {"url": "https://signed/a.pdf?sig=1"}

    f = _with_urls()
    calls = presign_calls(f, "give me the report link")[:1]
    run_followups(C(), f, calls)

    assert seen["path"].endswith("/presigned-url")
    assert seen["body"]["url"].endswith("a.pdf")
    assert seen["body"]["ttl"] == 3600
    assert any("signed/a.pdf" in str(v) for v in f.results.values())


def test_an_empty_presign_call_is_refused_before_it_is_sent():
    """It signs a URL, so it needs one; nothing in the path said so."""
    from oncallbot.tools.executor import ToolError, call_tool
    from oncallbot.tools.registry import read_only_tools

    spec = next(t for t in read_only_tools() if "presigned" in t.name)
    with pytest.raises(ToolError) as exc:
        call_tool(object(), spec, {})
    assert "url" in str(exc.value)


def test_the_answer_prompt_forbids_denying_capabilities():
    """The reported bug: it said "I don't have the ability to generate
    pre-signed URLs" — a capability it has, and cannot know it lacks."""
    from oncallbot.order_qa import ANSWER_SYSTEM_PROMPT as p

    assert "Never claim you cannot do something" in p
    assert "I don't have the ability to" in p
    assert "already been signed and is ready to open" in p
    assert "Quote it exactly as given" in p


def test_the_router_treats_a_report_link_as_an_order_lookup():
    from oncallbot.chat.intent import system_prompt

    p = system_prompt()
    assert "presigned URL" in p
    assert "they are lookups, not help" in p


def test_every_private_url_is_signed_not_only_on_a_link_question():
    """An answer to "who is the patient" can quote a report_url too, and a raw
    private URL 403s the moment it is clicked."""
    from oncallbot.order_qa import sign_private_urls

    class C:
        def post_read(self, path, body):
            return {"url": body["url"] + "?X-Amz-Signature=abc"}

    f = _with_urls()
    # Two of the three: the json dump already carries a signature.
    assert sign_private_urls(C(), f) == 2
    assert all("X-Amz-Signature" in v for v in f.signed.values())
    assert not any("c.json" in raw for raw in f.signed), "already signed, left alone"


def test_the_model_is_shown_the_signed_url_in_place_of_the_raw_one():
    """Substituted rather than appended: otherwise it can still quote the raw
    one, and the reader gets a dead link."""
    from oncallbot.order_qa import sign_private_urls

    class C:
        def post_read(self, path, body):
            return {"url": "https://signed/" + body["url"].rsplit("/", 1)[-1] + "?sig=1"}

    f = _with_urls()
    sign_private_urls(C(), f)
    context = f.context_for_model()

    assert "https://signed/a.pdf?sig=1" in context
    assert "1mg-droplet-production-internal.s3.ap-south-1.amazonaws.com/upload/reports/1/a.pdf" \
        not in context, "the raw URL must not survive in what the model sees"


def test_an_already_signed_url_is_not_signed_again():
    from oncallbot.order_qa import Fetched, sign_private_urls

    calls: list[str] = []

    class C:
        def post_read(self, path, body):
            calls.append(body["url"])
            return {"url": body["url"]}

    f = Fetched(order_group_id="PO1-11")
    f.results = {"x": {"report_url": "https://ok/a.pdf?X-Amz-Signature=already"}}
    assert sign_private_urls(C(), f) == 0
    assert calls == []


def test_a_failed_signing_leaves_the_url_raw_and_says_so():
    """Better a link that fails visibly than a claim it was signed."""
    from oncallbot.hra_client import HraError
    from oncallbot.order_qa import sign_private_urls

    class C:
        def post_read(self, path, body):
            raise HraError("403 from the signer")

    f = _with_urls()
    assert sign_private_urls(C(), f) == 0
    assert f.signed == {}
    assert any("403 from the signer" in e for e in f.errors)


def test_the_signers_answer_shape_does_not_have_to_be_guessed():
    from oncallbot.order_qa import _signed_url_from

    assert _signed_url_from({"url": "https://s/a"}) == "https://s/a"
    assert _signed_url_from({"signed_url": "https://s/b"}) == "https://s/b"
    assert _signed_url_from({"data": {"url": "https://s/c"}}) == "https://s/c"
    assert _signed_url_from("https://s/d") == "https://s/d"
    assert _signed_url_from({"error": "nope"}) == ""
    assert _signed_url_from(None) == ""
