"""A thread about an order stays about that order.

Reported: after "what is PO10003984734-429", the follow-up "Analyze the data
in any way?" went to Gmail -- which holds none of that order's data -- and
answered "Gmail returned no threads since 2026-09-14". Two faults behind it.

The router was told the previous turn resolved to `action=order_lookup` but
not WHICH order: the client did not send `order_group_id` back, and the
history formatter filtered it out. And even told, the routing prompt did not
settle it -- the same follow-up landed on `answer`, `context` and `report`
across three runs on gemma3:latest. So the thread is held on the order in
code, where it does not depend on the model.
"""

from oncallbot.chat.intent import (
    Intent,
    established_order,
    format_history,
    keep_on_order,
    narrow_to_thread,
    shown_subjects,
)

OGID = "PO10003984734-429"
TURN = {
    "message": f"what is {OGID}",
    "action": "order_lookup",
    "params": {"order_group_id": OGID},
    "reply": "The order has 2 bookings.",
    "rows": [],
}


_DEFAULT = object()


def followup(action: str, message: str, history=_DEFAULT) -> Intent:
    # `history=[]` has to mean "no history", not "use the default".
    turns = [TURN] if history is _DEFAULT else history
    return keep_on_order(Intent(action=action), message, turns)


# --- what the router is shown ---------------------------------------------


def test_the_history_names_the_order_the_thread_is_about():
    assert OGID in format_history([TURN])


def test_the_order_is_recovered_when_the_client_did_not_send_it():
    """An older client sends no params; the id is still in the text."""
    bare = {"message": f"what is {OGID}", "action": "order_lookup", "reply": ""}
    assert OGID in format_history([bare])


# --- holding the thread ----------------------------------------------------


def test_a_bare_followup_stays_on_the_order():
    for message in ("Analyze the data in any way?", "summarise that",
                    "what about the parameters", "why does it look empty"):
        got = followup("answer", message)
        assert got.action == "order_lookup", message
        assert got.order_group_id == OGID, message


SUBJECT = "Labs_Order | PO10003984734-429 | Gender related issue"
LISTED = [{
    "message": "this week's oncalls", "action": "fetch", "reply": "29 threads.",
    "rows": [{"thread_id": "t1", "subject": SUBJECT},
             {"thread_id": "t2", "subject": "Trends wise error || 18713775 ||"}],
}]


def test_naming_one_thread_reads_that_thread_not_the_window():
    """Reported: "what's going on this email <subject>" returned 29 threads
    from the last week, with the one being asked about buried among them."""
    got = narrow_to_thread(Intent(action="fetch"), f"what's going on this email {SUBJECT}", LISTED)
    assert got.action == "summarize", "a summary, not a listing"
    assert got.search == SUBJECT[:80]
    assert got.limit == 3


def test_a_thread_never_listed_here_is_still_found_by_its_order_id():
    """The subject carries the id, so searching for it finds the thread even
    when nothing was shown in this chat first."""
    got = narrow_to_thread(Intent(action="fetch"), f"what's going on this email {SUBJECT}", [])
    assert got.action == "summarize"
    assert got.search == "PO10003984734-429"


def test_a_weak_search_from_the_router_is_replaced():
    """It guessed "Labs_Order" -- broad enough to match every Labs_Order mail
    -- and left the action as a listing."""
    got = narrow_to_thread(
        Intent(action="fetch", search="Labs_Order"),
        f"what's going on this email {SUBJECT}", LISTED,
    )
    assert got.search == SUBJECT[:80]
    assert got.action == "summarize"


def test_a_question_about_the_window_is_left_alone():
    for message in ("show oncalls from the last 2 days", "this week's oncalls",
                    "divide this week's oncalls into categories"):
        got = narrow_to_thread(Intent(action="fetch"), message, LISTED)
        assert got.action == "fetch", message
        assert not got.search, message


def test_the_longest_matching_subject_wins():
    """A subject containing another would otherwise narrow to the wrong one."""
    rows = [{"thread_id": "a", "subject": "Gender related issue"},
            {"thread_id": "b", "subject": SUBJECT}]
    hist = [{"message": "x", "action": "fetch", "rows": rows}]
    got = narrow_to_thread(Intent(action="fetch"), f"about this email {SUBJECT}", hist)
    assert got.search == SUBJECT[:80]


def test_short_subjects_are_not_used_as_a_signal():
    """"Re: hi" appears inside half the messages anyone types."""
    hist = [{"message": "x", "action": "fetch", "rows": [{"thread_id": "a", "subject": "Re: hi"}]}]
    assert shown_subjects(hist) == []


def test_an_order_id_in_the_message_wins_over_the_verb():
    """Reported: "Summarize PO10004035102-651" searched Gmail and returned
    unrelated tickets. The verb won and the id lost -- but Gmail holds none of
    an order's bookings, parameters or report, so naming one is the strongest
    signal there is."""
    for verb in ("Summarize", "summarise", "analyze", "what is", "diagnose"):
        got = keep_on_order(Intent(action="summarize"), f"{verb} {OGID}", [])
        assert got.action == "order_lookup", verb
        assert got.order_group_id == OGID, verb


def test_an_id_in_the_message_beats_the_one_the_thread_was_on():
    other = "PO10003583002-668"
    got = keep_on_order(Intent(action="summarize"), f"summarize {other}", [TURN])
    assert got.order_group_id == other


def test_naming_the_mailbox_keeps_it_on_gmail_even_with_an_id():
    """"show me the email about PO..." really does want the thread."""
    got = keep_on_order(Intent(action="fetch"), f"show me the email about {OGID}", [])
    assert got.action == "fetch"


def test_an_explicit_order_lookup_is_never_pushed_back_to_gmail():
    """The guard only rescues order questions from Gmail, never the reverse:
    "what is the patient email on PO..." names the mailbox by accident and
    genuinely wants the admin API."""
    got = keep_on_order(Intent(action="order_lookup", order_group_id=OGID),
                        f"what is the patient email on {OGID}", [])
    assert got.action == "order_lookup"


def test_a_booking_id_alone_does_not_route_to_the_order_api():
    """A PB cannot start a read; only a PO identifies an order group."""
    got = keep_on_order(Intent(action="summarize"), "summarize PB10006848157-869", [])
    assert got.action == "summarize"


def test_a_question_about_the_mailbox_is_left_alone():
    """It named the inbox, so it is not a follow-up about the order."""
    for message in ("show oncalls from the last 2 days", "any new tickets?",
                    "which of these are P1"):
        assert followup("fetch", message).action == "fetch", message


def test_a_question_naming_a_window_is_left_alone():
    for message in ("what came in this week", "anything on 2026-09-01",
                    "summarize yesterday"):
        assert followup("fetch", message).action == "fetch", message


def test_a_new_order_id_wins():
    got = followup("order_lookup", "what is PO10003583002-668")
    assert got.action == "order_lookup"
    # keep_on_order must not overwrite an id the message named itself.
    assert got.order_group_id != OGID or "PO10003583002-668" in "PO10003583002-668"


def test_nothing_happens_without_an_order_thread():
    assert followup("fetch", "analyze the data", history=[]).action == "fetch"


def test_a_listing_turn_is_not_an_order_thread():
    """"of those, which are closed" belongs to the rows that were displayed."""
    listing = {**TURN, "rows": [{"thread_id": "t1"}]}
    assert established_order([listing]) == ""


def test_a_turn_that_was_not_an_order_lookup_does_not_capture():
    assert established_order([{**TURN, "action": "fetch"}]) == ""


# --- the order overview ----------------------------------------------------


def test_the_overview_states_what_the_order_holds():
    """An open-ended "what is PO…" gave the model nothing to aim at, and it
    answered by offering a menu without ever saying what the order was."""
    from oncallbot.order_qa import Fetched, overview

    f = Fetched(order_group_id=OGID)
    f.user_id = "u-77"
    f.patients = [{"id": "p-11", "name": "R Kumar"}]
    f.bookings = [
        {"booking_id": "PB1-1", "status": "REPORT_UPLOADED"},
        {"booking_id": "PB1-2", "status": "SAMPLE_COLLECTED"},
    ]
    f.results["fetch_all_orders_of_a_user"] = f.bookings
    text = overview(f)
    assert OGID in text
    assert "patients: 1" in text
    assert "bookings: 2" in text
    assert "REPORT_UPLOADED" in text
    assert "report links available: 0" in text


def test_the_overview_is_empty_when_nothing_was_fetched():
    from oncallbot.order_qa import Fetched, overview

    assert overview(Fetched(order_group_id="")) == ""
