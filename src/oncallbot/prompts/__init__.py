"""Every prompt this tool sends, grouped by the feature that sends it.

One place to read them, because a prompt is behaviour: most of what this tool
gets right or wrong is decided by the wording in here, and it used to be
spread across eight modules where you could only find it by grepping.

    feature                  module        prompts
    ------------------------------------------------------------------------
    routing a message        routing.py    SYSTEM_PROMPT_TEMPLATE
    summarizing a thread     summarize.py  SYSTEM_PROMPT, build_user_prompt()
    categorizing a window    grouping.py   GROUPING_SYSTEM_PROMPT
    answering in chat        chat.py       CONTEXT_SYSTEM_PROMPT
                                           ANSWER_SYSTEM_PROMPT
    order / booking reads    order_qa.py   PLAN_SYSTEM_PROMPT
                                           ANSWER_SYSTEM_PROMPT
    diagnosis (phase 2)      diagnosis.py  REASON_SYSTEM_PROMPT
                                           FINDINGS_SYSTEM_PROMPT
                                           CLOSURE_SYSTEM_PROMPT
                                           FOLLOWUP_PLAN_SYSTEM_PROMPT
                                           FOLLOWUP_SYSTEM_PROMPT
    suggestion chips         suggest.py    SYSTEM_PROMPT

`ANSWER_SYSTEM_PROMPT` deliberately exists twice: answering over triage
summaries and answering over admin-API payloads are different jobs with
different rules. They are not re-exported here for that reason -- import from
the feature module, so which one you mean is written down at the import.

What is NOT here, and must not move here: anything that decides an outcome.
Verdicts come from exact comparison in `diagnose.py`, counts from `len()` in
`grouping.py` and `chat/facts.py`, tool prerequisites from
`tools/registry.py`. The prompts narrate those decisions; they never make
them, and several say so in their own text.
"""

from __future__ import annotations

# Re-exported because three backends import them from here by their old path.
from .summarize import SYSTEM_PROMPT, build_user_prompt

__all__ = ["SYSTEM_PROMPT", "build_user_prompt"]
