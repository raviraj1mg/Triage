"""A failed `claude` run has to say what to do about it.

A teammate who had followed every README step, unzipped, and signed in with
Google saw this on their first question:

  Could not understand that: {"duration_api_ms":0,"stop_reason":"stop_sequence",
  "session_id":"9a6019a1-...","total_cost_usd":0,"usage":{...,"server_tool_use":{"web_sear

That is the CLI's result envelope, written to stdout even on failure, cut at
300 characters. Their stderr was empty, so it was all the caller had. The
cause was a Claude Code that was installed but had no usable credentials --
which `which("claude")` cannot see.
"""

from oncallbot.streaming import _human_part, explain_cli_failure

# Verbatim shape from the report: telemetry only, no human field at all.
TELEMETRY = (
    '{"duration_api_ms":0,"stop_reason":"stop_sequence",'
    '"session_id":"9a6019a1-7575-4e51-a4e7-5bd1f7546758","total_cost_usd":0,'
    '"usage":{"output_tokens_details":{"thinking_tokens":0},"input_tokens":0,'
    '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    '"output_tokens":0,"server_tool_use":{"web_search_requests":0}}}'
)


def test_the_reported_envelope_does_not_reach_the_reader():
    """Token counts, a session id and a duration tell nobody anything."""
    out = explain_cli_failure(TELEMETRY, "", 1)
    assert "duration_api_ms" not in out
    assert "session_id" not in out
    assert "total_cost_usd" not in out


def test_an_envelope_with_nothing_useful_says_what_to_run():
    out = explain_cli_failure(TELEMETRY, "", 1)
    assert "claude -p hello" in out, "give them a way to see the real reason"
    assert "signing in" in out


def test_a_login_failure_is_named():
    out = explain_cli_failure(
        '{"type":"result","is_error":true,"result":"Invalid API key · Please run /login"}',
        "", 1,
    )
    assert "not signed in" in out
    assert "finish the login" in out
    # The CLI's own words are kept, after the advice rather than instead of it.
    assert "Please run /login" in out


def test_a_usage_limit_is_not_reported_as_a_login_problem():
    out = explain_cli_failure(
        '{"result":"Claude usage limit reached. Try again later."}', "", 1)
    assert "usage limit" in out
    assert "not signed in" not in out
    assert "anthropic_api" in out, "offer the way out"


def test_an_old_cli_is_told_to_update():
    out = explain_cli_failure("", "error: unknown option '--tools'", 2)
    assert "too old" in out
    assert "npm install -g" in out


def test_stderr_is_used_when_stdout_is_not_json():
    out = explain_cli_failure("not json at all", "", 3)
    assert "not json at all" in out


def test_the_human_part_is_pulled_out_of_a_nested_error():
    assert _human_part('{"error":{"message":"boom"}}') == "boom"


def test_the_human_part_reads_the_last_stream_json_line():
    """stream-json is one object per line; the outcome is the last one."""
    text = '{"type":"stream_event"}\n{"type":"result","result":"went wrong"}'
    assert _human_part(text) == "went wrong"


def test_the_human_part_is_empty_when_there_is_only_telemetry():
    """So the caller falls back to advice rather than printing numbers."""
    assert _human_part(TELEMETRY) == ""
