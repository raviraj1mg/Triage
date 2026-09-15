"""Only URLs the order's own data returned may reach the UI.

Measured on gemma3:latest: given a report URL for one booking and nothing for
the other, it wrote `.../reports/1/b.pdf?X-Amz-Signature=...` for the second by
following the shape of the first, in 5 of 5 runs, and captioned it as a report
link that expires in an hour. The ANSWER prompt already says never to rebuild
a URL, so the rule has to hold somewhere a model cannot ignore it -- a
fabricated S3 key is not a dead link, it can name a real object belonging to a
different patient, and the UI turns every URL into a click-through.
"""

from oncallbot.order_qa import (
    Fetched,
    NO_URL,
    allowed_urls,
    allowed_urls_in,
    scrub_urls,
)

GOOD = "https://x.s3.amazonaws.com/upload/reports/1/a.pdf?X-Amz-Signature=abc"
BAD = "https://x.s3.amazonaws.com/upload/reports/1/b.pdf?X-Amz-Signature=abc"
RAW = "https://x.s3.amazonaws.com/upload/reports/1/c.pdf"
SIGNED_C = RAW + "?X-Amz-Signature=zzz"


def run(chunks, allowed):
    return "".join(scrub_urls(iter(chunks), allowed))


def test_a_url_from_the_data_passes_through():
    assert run(["see ", GOOD, " ok"], {GOOD: GOOD}) == f"see {GOOD} ok"


def test_a_url_the_data_never_returned_is_replaced():
    assert run(["see ", BAD, " ok"], {GOOD: GOOD}) == f"see {NO_URL} ok"


def test_it_survives_arriving_one_character_at_a_time():
    """The real path is a token stream, so the URL is split across chunks."""
    assert run(list(f"a {GOOD} b"), {GOOD: GOOD}) == f"a {GOOD} b"
    assert run(list(f"a {BAD} b"), {GOOD: GOOD}) == f"a {NO_URL} b"


def test_a_url_at_the_very_end_is_still_checked():
    """No terminator ever arrives, so the flush has to decide."""
    assert run([f"end {BAD}"], {GOOD: GOOD}) == f"end {NO_URL}"
    assert run([f"end {GOOD}"], {GOOD: GOOD}) == f"end {GOOD}"


def test_punctuation_after_a_url_survives_the_replacement():
    assert run([f"link: {BAD}."], {GOOD: GOOD}) == f"link: {NO_URL}."
    assert run([f"({BAD})"], {GOOD: GOOD}) == f"({NO_URL})"


def test_backticked_urls_keep_their_backticks():
    assert run([f"`{GOOD}`"], {GOOD: GOOD}) == f"`{GOOD}`"


def test_the_word_http_in_prose_is_not_a_url():
    """It would otherwise be replaced with the "not in the data" note."""
    assert run(["talk about http and things"], {}) == "talk about http and things"
    assert run(["ends in http"], {}) == "ends in http"


def test_text_with_no_urls_is_untouched():
    assert run(["no urls here at all"], {GOOD: GOOD}) == "no urls here at all"


def test_a_raw_url_is_upgraded_to_its_signed_form():
    """The model may quote the unsigned one; the engineer needs the signed."""
    allowed = allowed_urls_in({"a": {"report_url": RAW}}, {RAW: SIGNED_C})
    assert run([f"open {RAW} now"], allowed) == f"open {SIGNED_C} now"


def test_allowed_urls_does_not_depend_on_the_key_name():
    """A URL under an oddly named key is still one the data returned."""
    allowed = allowed_urls_in({"a": {"some_odd_key": GOOD}})
    assert GOOD in allowed
    assert run([f"see {GOOD}"], allowed) == f"see {GOOD}"


def test_allowed_urls_reads_a_fetched():
    f = Fetched(order_group_id="PO1-1")
    f.results["fetch_all_orders_of_a_user"] = [{"report_url": GOOD}]
    assert GOOD in allowed_urls(f)


# --- the link-expiry note --------------------------------------------------


def test_the_expiry_note_goes_when_the_data_held_no_link():
    """"Report links expire in about an hour" under an answer containing no
    link reads as though one were given, and sends the reader hunting for it.

    The prompt asks for it only alongside a link; gemma3:latest said it anyway,
    so when the order returned no URL the sentence is removed here -- a
    decision that needs no model, because there is nothing that could expire.
    """
    from oncallbot.order_qa import drop_link_notes

    out = "".join(drop_link_notes(iter([
        "The order has three parameters. Report links expire in about an hour.\n"
    ])))
    assert "expire" not in out
    assert "three parameters" in out


def test_a_line_that_was_only_the_note_goes_entirely():
    from oncallbot.order_qa import drop_link_notes

    out = "".join(drop_link_notes(iter(["Nitrite: negative.\nreport links expire in about an hour\n"])))
    assert "expire" not in out
    assert "Nitrite" in out


def test_the_note_survives_character_by_character_streaming():
    from oncallbot.order_qa import drop_link_notes

    text = "Done. Report links expire in about an hour.\n"
    assert "expire" not in "".join(drop_link_notes(iter(list(text))))
