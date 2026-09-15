"""oncallbot CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import DEFAULT_CONFIG_PATH, DEFAULT_PORT, Config, load_config
from .gmail_auth import WrongAccountError, assert_account, build_service, get_credentials
from .gmail_client import GmailClient
from . import process
from .query import build_query
from .render import print_table, to_json, to_markdown
from .store import Store
from .summarizer import SummarizerError, build_summarizer

app = typer.Typer(
    add_completion=False,
    help="Triage the health-record support inbox: find tagged threads, summarize the issue.",
)
console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yaml.")]


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except (FileNotFoundError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


def _client(cfg: Config, *, interactive: bool = False) -> GmailClient:
    try:
        service = build_service(
            cfg.gmail.credentials_file,
            cfg.gmail.token_file,
            interactive=interactive,
            login_hint=cfg.gmail.account,
        )
        mailbox = assert_account(service, cfg.gmail.account)
    except WrongAccountError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    except (FileNotFoundError, RuntimeError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    err.print(f"[dim]mailbox: {mailbox}[/dim]")
    return GmailClient(service)


@app.command()
def auth(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Run the one-time OAuth consent and cache the token."""
    cfg = _load(config)
    if cfg.gmail.account:
        console.print(f"Consent as [bold]{cfg.gmail.account}[/bold] on the browser page.")
    try:
        get_credentials(
            cfg.gmail.credentials_file,
            cfg.gmail.token_file,
            interactive=True,
            login_hint=cfg.gmail.account,
        )
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    # Confirm which mailbox actually consented -- the login_hint is only a hint.
    try:
        _client(cfg)
    except typer.Exit:
        err.print(
            f"[yellow]Consent completed but the wrong account was used. "
            f"Delete {cfg.gmail.token_file} and try again.[/yellow]"
        )
        raise
    console.print(f"[green]Authorized.[/green] Token cached at {cfg.gmail.token_file}")


@app.command("query")
def show_query(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Print the Gmail search query the current config produces."""
    cfg = _load(config)
    console.print(build_query(cfg.gmail))


@app.command()
def fetch(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    days: Annotated[Optional[int], typer.Option(help="Override lookback_days.")] = None,
    after: Annotated[
        Optional[str], typer.Option(help="Window start, YYYY-MM-DD. Overrides --days.")
    ] = None,
    before: Annotated[
        Optional[str], typer.Option(help="Window end, YYYY-MM-DD, exclusive.")
    ] = None,
    limit: Annotated[Optional[int], typer.Option(help="Override max_threads.")] = None,
) -> None:
    """List matching threads without calling the model. Use this to tune matchers."""
    cfg = _load(config)
    if days is not None:
        cfg.gmail.lookback_days = days
    if limit is not None:
        cfg.gmail.max_threads = limit
    if after or before:
        from datetime import date as _date

        for label, value in (("--after", after), ("--before", before)):
            if value:
                try:
                    _date.fromisoformat(value)
                except ValueError:
                    err.print(f"[red]{label} must be YYYY-MM-DD, got {value!r}.[/red]")
                    raise typer.Exit(2)
        cfg.gmail.after = after or ""
        cfg.gmail.before = before or ""
        cfg.gmail.lookback_days = 0

    q = build_query(cfg.gmail)
    console.print(f"[dim]query:[/dim] {q}\n")

    client = _client(cfg)
    count = 0
    for thread in client.iter_threads(
        q,
        max_threads=cfg.gmail.max_threads,
        include_spam_trash=cfg.gmail.include_spam_trash,
    ):
        count += 1
        last = thread.last
        when = last.date.strftime("%Y-%m-%d %H:%M") if last.date else "?"
        console.print(f"[bold]{thread.subject}[/bold]")
        console.print(
            f"  [dim]{thread.id} · {len(thread.messages)} msg · {when} · {last.sender}[/dim]"
        )
    console.print(f"\n[green]{count}[/green] thread(s) matched.")


@app.command()
def summarize(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    days: Annotated[Optional[int], typer.Option(help="Override lookback_days.")] = None,
    after: Annotated[
        Optional[str], typer.Option(help="Window start, YYYY-MM-DD. Overrides --days.")
    ] = None,
    before: Annotated[
        Optional[str], typer.Option(help="Window end, YYYY-MM-DD, exclusive.")
    ] = None,
    limit: Annotated[Optional[int], typer.Option(help="Override max_threads.")] = None,
    thread: Annotated[
        Optional[str],
        typer.Option(help="Summarize one thread id and ignore the query."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Re-summarize threads already cached.")] = False,
    fmt: Annotated[
        str, typer.Option("--format", help="table | markdown | json")
    ] = "table",
    out: Annotated[Optional[Path], typer.Option(help="Write output to a file too.")] = None,
) -> None:
    """Fetch matching threads, summarize each into a structured issue, cache the result."""
    cfg = _load(config)
    if days is not None:
        cfg.gmail.lookback_days = days
    if limit is not None:
        cfg.gmail.max_threads = limit
    if after or before:
        from datetime import date as _date

        for label, value in (("--after", after), ("--before", before)):
            if value:
                try:
                    _date.fromisoformat(value)
                except ValueError:
                    err.print(f"[red]{label} must be YYYY-MM-DD, got {value!r}.[/red]")
                    raise typer.Exit(2)
        cfg.gmail.after = after or ""
        cfg.gmail.before = before or ""
        cfg.gmail.lookback_days = 0

    client = _client(cfg)
    summarizer = build_summarizer(cfg, client)

    if thread:
        threads = [client.get_thread(thread)]
    else:
        q = build_query(cfg.gmail)
        err.print(f"[dim]query: {q}[/dim]")
        threads = list(
            client.iter_threads(
                q,
                max_threads=cfg.gmail.max_threads,
                include_spam_trash=cfg.gmail.include_spam_trash,
            )
        )

    results = []
    failures = 0
    with Store(cfg.store_path) as store:
        for i, th in enumerate(threads, 1):
            cached = store.get(th.id)
            if not force and cached and store.is_current(th.id, th.last.id):
                err.print(f"[dim]({i}/{len(threads)}) cached: {th.subject[:60]}[/dim]")
                results.append(cached)
                continue

            err.print(f"[dim]({i}/{len(threads)}) summarizing: {th.subject[:60]}[/dim]")
            try:
                summary = summarizer.summarize(th)
            except SummarizerError as exc:
                failures += 1
                err.print(f"[red]  failed: {exc}[/red]")
                continue
            store.upsert(summary, th.last.id)
            results.append(summary.to_dict())

    _emit(results, fmt, out)
    if failures:
        err.print(f"[yellow]{failures} thread(s) failed to summarize.[/yellow]")
        raise typer.Exit(1)


@app.command()
def report(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    limit: Annotated[int, typer.Option(help="How many cached summaries to show.")] = 50,
    fmt: Annotated[str, typer.Option("--format", help="table | markdown | json")] = "table",
    out: Annotated[Optional[Path], typer.Option(help="Write output to a file too.")] = None,
) -> None:
    """Render a digest from summaries already cached. No Gmail or model calls."""
    cfg = _load(config)
    with Store(cfg.store_path) as store:
        _emit(store.recent(limit), fmt, out)


def _emit(items: list[dict], fmt: str, out: Path | None) -> None:
    if fmt == "table":
        print_table(items, console)
        text = to_markdown(items)  # tables do not round-trip to a file
    elif fmt == "markdown":
        text = to_markdown(items)
        console.print(text)
    elif fmt == "json":
        text = to_json(items)
        sys.stdout.write(text + "\n")
    else:
        err.print(f"[red]Unknown --format {fmt!r}. Use table, markdown or json.[/red]")
        raise typer.Exit(2)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        err.print(f"[green]Wrote {out}[/green]")


@app.command()
def order(
    question: Annotated[
        str,
        typer.Argument(help='What you want to know, e.g. "who is the patient on PO123?"'),
    ],
    order_group_id: Annotated[
        Optional[str],
        typer.Option("--order", help="Order group id, if it is not in the question."),
    ] = None,
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    show_data: Annotated[
        bool, typer.Option(help="Print the raw API responses as well.")
    ] = False,
) -> None:
    """Ask about an order. Uses the read-only APIs in tools/order_info.md."""
    from .hra_client import HraAuthError, HraBlockedError, HraClient
    from .order_qa import answer_stream, find_order_ids, gather_entry, plan_followups, run_followups

    cfg = _load(config)

    ogid = order_group_id or next(iter(find_order_ids(question)), "")
    if not ogid:
        err.print(
            "[yellow]I need an order group id to look anything up.[/yellow]\n"
            "Pass it in the question or with --order, e.g. "
            "[bold]oncallbot order \"who is the patient?\" --order PO10003583002-668[/bold]"
        )
        raise typer.Exit(2)

    console.print(f"[dim]order {ogid}[/dim]")
    client = HraClient(cfg.hra)
    try:
        try:
            fetched = gather_entry(
                client, ogid, on_progress=lambda m: err.print(f"[dim]· {m}[/dim]")
            )
        except (HraAuthError, HraBlockedError) as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc

        calls = plan_followups(cfg, question, fetched)
        if calls:
            err.print(f"[dim]· {len(calls)} follow-up call(s)[/dim]")
            run_followups(
                client, fetched, calls,
                on_progress=lambda m: err.print(f"[dim]· {m}[/dim]"),
            )

        if not fetched.results:
            err.print("[red]No data came back for that order.[/red]")
            for e in fetched.errors:
                err.print(f"  [red]{e}[/red]")
            raise typer.Exit(1)

        console.print()
        for chunk in answer_stream(cfg, question, fetched):
            console.print(chunk, end="")
        console.print()

        if fetched.errors:
            console.print()
            for e in fetched.errors:
                err.print(f"[yellow]· {e}[/yellow]")
        if show_data:
            console.print()
            console.print("[dim]" + fetched.context_for_model() + "[/dim]")
    finally:
        client.close()


@app.command()
def diagnose(
    order_group_id: Annotated[
        str, typer.Argument(help="The order group id from the ticket, e.g. PO10003583002-668.")
    ],
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    reason: Annotated[
        bool, typer.Option(help="Also ask the model to narrate the verdict.")
    ] = False,
    fmt: Annotated[str, typer.Option("--format", help="text | json")] = "text",
    raw: Annotated[
        bool, typer.Option(help="Dump each admin API response instead of diagnosing.")
    ] = False,
) -> None:
    """Diagnose one order against the edit-patient runbooks. Reads only."""
    from .diagnose import (
        INDETERMINATE,
        MATCH,
        MISMATCH,
        diagnose_order,
        strip_code_marks,
    )
    from .hra_client import HraClient

    cfg = _load(config)

    if raw:
        _diagnose_raw(cfg, order_group_id)
        return

    client = HraClient(cfg.hra)
    try:
        d = diagnose_order(
            cfg,
            order_group_id,
            client=client,
            on_progress=lambda m: err.print(f"[dim]· {m}[/dim]"),
            with_reason=reason,
        )
    finally:
        client.close()

    if fmt == "json":
        sys.stdout.write(json.dumps(d.to_dict(), indent=2, default=str) + "\n")
        raise typer.Exit(0 if d.verdict != INDETERMINATE else 1)

    style = {MISMATCH: "bold red", MATCH: "green", INDETERMINATE: "yellow"}[d.verdict]
    state = (
        "[dim]EMAIL CLOSED[/dim]" if d.thread_closed else "[yellow]EMAIL OPEN[/yellow]"
    )
    console.print()
    console.print(
        f"[{style}]{d.verdict.upper()}[/]  {state}"
        + (f"  ·  {d.runbook}" if d.runbook else "")
    )
    if d.closure_reason:
        console.print(f"[dim]{d.closure_reason.rstrip('.')}.[/dim]")
    if d.thread_closed and d.closed_by:
        console.print(
            f"[dim]Closed by {d.closed_by}"
            + (f" on {d.closed_at}" if d.closed_at else "")
            + f" · confidence {d.closure_confidence:.2f}[/dim]"
        )
    console.print()

    if d.verdict == INDETERMINATE:
        console.print(f"[yellow]Could not establish:[/yellow] {d.blocked_because}")
        if d.auth_expired:
            console.print(
                "[yellow]This is a credential problem, not an inconclusive "
                "diagnosis.[/yellow]"
            )
        if d.trail:
            console.print("\n[dim]Got as far as:[/dim]")
            for step in d.trail:
                console.print(f"  [dim]· {step}[/dim]")
        raise typer.Exit(1)

    ids = Table(show_header=False, box=None, pad_edge=False)
    ids.add_column(style="dim")
    ids.add_column()
    ids.add_row("order_group_id", d.order_group_id)
    ids.add_row("user_id", d.user_id)
    ids.add_row("patient_id", d.patient_id)
    ids.add_row("booking_id", d.booking_id)
    ids.add_row("report format", d.report_format)
    console.print(ids)
    console.print()

    cmp_table = Table(show_lines=False, header_style="bold", box=None, pad_edge=False)
    cmp_table.add_column("Field")
    cmp_table.add_column("On the report")
    cmp_table.add_column("In patientsv2")
    cmp_table.add_column("Verdict")
    for c in d.comparisons:
        cmp_table.add_row(
            c.field,
            c.report_value or "[dim](empty)[/dim]",
            c.record_value or "[dim](empty)[/dim]",
            "[green]match[/green]" if c.matches else "[red]MISMATCH[/red]",
        )
    console.print(cmp_table)

    for label, created, changes in (
        ("Name history", d.name_created_as, d.name_changes),
        ("Gender history", d.gender_created_as, d.gender_changes),
    ):
        if not (created or changes):
            continue
        console.print()
        console.print(f"[bold]{label}[/bold]")
        console.print(f"  at creation: {created or '[dim]unknown[/dim]'}")
        for ch in changes:
            actor = f" by {ch.actor_id}" if ch.actor_id else ""
            console.print(
                f"  {ch.from_value or '?'} → {ch.to_value or '?'} "
                f"at {ch.changed_at or 'unknown'}{actor}"
            )

    if d.checks:
        console.print()
        console.print("[bold]Runbook checks[/bold]")
        for c in d.checks:
            mark = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
            console.print(f"  {mark} {c.label}")
            if c.detail:
                console.print(f"    [dim]{strip_code_marks(c.detail)}[/dim]")

    if d.reason:
        console.print()
        console.print("[bold]Why it failed[/bold]")
        console.print(_block(d.reason))

    if d.other_findings:
        console.print()
        console.print("[bold]No runbook fired — what the evidence does show[/bold]")
        console.print(_block(d.other_findings))

    if d.followup_question or d.followup_findings:
        console.print()
        console.print("[bold]Further check[/bold]")
        console.print(f"  [dim]{escape(d.followup_question)}[/dim]")
        for c in d.followup_calls:
            label = f"{c.get('tool')}({c.get('params')})" if c.get("tool") else "no call"
            console.print(f"  [dim]· {escape(label)} → {escape(c.get('outcome',''))}[/dim]")
        if d.followup_findings:
            console.print(_block(d.followup_findings))

    if d.resolution:
        console.print()
        console.print("[bold]Resolution — dashboard only[/bold]")
        for i, step in enumerate(d.resolution, 1):
            console.print(f"  {i}. {strip_code_marks(step)}")

    console.print()


def _block(text: str) -> str:
    """Indent a model-written block for the terminal, bullet list and all.

    The prose is a bullet list written for the web UI. Printing it with a
    single leading indent would align only the first line, and rich would read
    any square brackets in it as markup -- so every line is indented and the
    whole block is escaped.
    """
    from .diagnose import strip_code_marks

    lines = [ln.rstrip() for ln in strip_code_marks(text).strip().splitlines()]
    return "\n".join(f"  {escape(ln)}" if ln else "" for ln in lines)


def _diagnose_raw(cfg: Config, order_group_id: str) -> None:
    """Print each admin API response verbatim.

    For the first live run: it shows which keys the real payloads actually use,
    which is what the diagnosis depends on and what fixtures get wrong.
    """
    from .hra_client import HraClient, HraError

    client = HraClient(cfg.hra)
    try:
        def show(label: str, fn) -> object:
            console.print(f"\n[bold]{label}[/bold]")
            try:
                out = fn()
            except HraError as exc:
                console.print(f"  [red]{exc}[/red]")
                return None
            console.print(json.dumps(out, indent=2, default=str)[:2600])
            return out

        details = show(
            f"GET /users/order/{order_group_id}",
            lambda: client.user_details(order_group_id),
        )
        user_id = str((details or {}).get("user_id") or "")
        if not user_id:
            console.print("\n[yellow]No user_id — stopping.[/yellow]")
            return

        orders = show(
            f"GET /user/{user_id}/orders?order_group_id={order_group_id}",
            lambda: client.orders(user_id, order_group_id),
        )
        rows = orders if isinstance(orders, list) else (orders or {}).get("orders") or []
        first = next((r for r in rows if isinstance(r, dict)), {})
        booking_id = str(first.get("booking_id") or first.get("id") or "")
        patient_id = str(first.get("patient_id") or "")
        console.print(
            f"\n[dim]booking row keys:[/dim] {sorted(first.keys()) if first else '(none)'}"
        )

        if booking_id:
            show(
                f"GET /booking/{booking_id}/json-report",
                lambda: client.json_report_url(booking_id, order_group_id),
            )
        if patient_id:
            show(
                f"GET /patient/{patient_id}/versions",
                lambda: client.patient_versions(patient_id),
            )
    finally:
        client.close()


@app.command()
def doctor(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    """Check the setup and say exactly what is missing. Run this first."""
    import shutil as _shutil

    # ok=True passed, ok=False broken, ok=None "not done yet, and that is
    # fine". The third state matters: a fresh install has nobody signed in,
    # which is expected rather than a fault to fix.
    checks: list[tuple[str, bool | None, str, str]] = []

    def add(name: str, ok: bool | None, note: str = "", then: str = "") -> None:
        """`then` is what to do about a pending item, if anything."""
        checks.append((name, ok, note, then))

    # 1. config file
    cfg = None
    if not config.exists():
        add("config file", False, f"{config} missing — run: cp config.example.yaml {config}")
    else:
        try:
            cfg = load_config(config)
            add("config file", True, str(config))
        except Exception as exc:  # noqa: BLE001 - the message is the point
            add("config file", False, str(exc).splitlines()[0])

    if cfg is not None:
        login = cfg.auth.enabled

        # 2. mailbox. Under login the account that consents IS the identity,
        # so config.yaml naming one is optional.
        acct = cfg.gmail.account
        placeholder = bool(acct) and ("your.name" in acct or "example" in acct)
        if placeholder:
            add("gmail.account", False,
                f"{acct} is still the placeholder — change it or leave it empty")
        elif acct:
            add("gmail.account", True,
                acct + (" (consent hint, and the account the CLI uses)" if login else ""))
        elif login:
            add("gmail.account", None,
                "empty — fine with login on: whoever signs in is the account")
        else:
            add("gmail.account", False,
                "not set — put your own @1mg.com address in config.yaml")

        add("support address", True, cfg.gmail.support_address)

        # 3. the OAuth client, which the browser sign-in needs
        add(
            "OAuth client secrets",
            cfg.gmail.credentials_file.exists(),
            str(cfg.gmail.credentials_file)
            + ("" if cfg.gmail.credentials_file.exists() else " missing — see README → Setup step 2"),
        )

        # 4. who is authorized. Two independent stores: the browser sessions'
        # per-user tokens, and the CLI's single one.
        if login:
            signed_in = sorted(cfg.auth.tokens_dir.glob("*.json"))
            add(
                "browser sign-in",
                True if signed_in else None,
                f"{len(signed_in)} account(s) authorized in {cfg.auth.tokens_dir}"
                if signed_in
                else "nobody has signed in yet",
                then="" if signed_in else "run `oncallbot serve` and sign in with Google",
            )
            add("OAuth redirect", True,
                f"http://localhost:{DEFAULT_PORT}{cfg.auth.redirect_path} (loopback needs no "
                "registration; a hosted URL does)")

        has_token = cfg.gmail.token_file.exists()
        add(
            "CLI token",
            has_token or (None if login else False),
            str(cfg.gmail.token_file) if has_token else "not authorized",
            then="" if has_token
            else "run `oncallbot auth` only if you want the terminal commands",
        )

        # 5. the mailbox the CLI token actually opens
        if has_token:
            try:
                service = build_service(
                    cfg.gmail.credentials_file, cfg.gmail.token_file, interactive=False
                )
                mailbox = assert_account(service, cfg.gmail.account)
                add("token matches account", True, mailbox)
            except WrongAccountError as exc:
                add("token matches account", False, str(exc).splitlines()[0])
            except Exception as exc:  # noqa: BLE001
                add("token matches account", False, f"{type(exc).__name__}: {exc}")

        # 5. the model backend
        backend = cfg.summarizer.backend
        if backend == "claude_cli":
            found = _shutil.which("claude")
            add(
                "claude CLI",
                found is not None,
                found or "not on PATH — install Claude Code, or switch to "
                "summarizer.backend: anthropic_api",
            )
            if found:
                # Being on PATH is not the same as being usable: a Claude Code
                # that has never been signed in passes `which` and then fails
                # every question with its own telemetry. One trivial call is
                # what separates "installed" from "works".
                from .streaming import probe_cli

                ok, note = probe_cli()
                add(
                    "claude CLI answers",
                    ok,
                    note,
                    "" if ok else
                    "Run `claude` once and finish the login, or put "
                    "ANTHROPIC_API_KEY in .env — the CLI picks it up too.",
                )
        elif backend == "anthropic_api":
            try:
                cfg.summarizer.api_key()
                add(
                    "Anthropic API key",
                    True,
                    f"${cfg.summarizer.api_key_env} is set → model {cfg.summarizer.api_model}",
                )
            except Exception as exc:  # noqa: BLE001
                add("Anthropic API key", False, str(exc).splitlines()[0])
        else:
            # A local server is either up or it is not; ask it.
            import httpx as _httpx

            target = f"{cfg.local.base_url} → {cfg.local.model}"
            try:
                r = _httpx.get(f"{cfg.local.base_url.rstrip('/')}/v1/models", timeout=5.0)
                if r.status_code >= 400:
                    add("local model server", False,
                        f"{cfg.local.base_url} answered {r.status_code} — is it "
                        "OpenAI-compatible?")
                else:
                    names = []
                    try:
                        names = [m.get("id", "") for m in (r.json().get("data") or [])]
                    except Exception:  # noqa: BLE001
                        pass
                    if names and cfg.local.model not in names:
                        add("local model server", False,
                            f"up, but {cfg.local.model} is not loaded. Available: "
                            + ", ".join(names[:5]))
                    else:
                        add("local model server", True, target)
            except Exception as exc:  # noqa: BLE001
                add("local model server", False,
                    f"cannot reach {cfg.local.base_url} ({type(exc).__name__}) — "
                    "start it, e.g. `ollama serve`")

        # 6. writable state
        for label, path in (
            ("store directory", cfg.store_path.parent),
            *((("per-user stores", cfg.auth.stores_dir),
               ("token directory", cfg.auth.tokens_dir)) if login else ()),
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
                add(label, True, str(path))
            except OSError as exc:
                add(label, False, str(exc))

        add(
            "attachment downloads",
            True,
            "on — PHI leaves this machine, see docs/phi.md"
            if cfg.attachments.enabled
            else "off",
        )
        add("PII redaction", True, "on" if cfg.redaction_enabled else "OFF — see docs/phi.md")

    console.print()
    for name, ok, note, _then in checks:
        mark = (
            "[green]✓[/green]" if ok is True
            else "[yellow]·[/yellow]" if ok is None
            else "[red]✗[/red]"
        )
        console.print(f" {mark} [bold]{name}[/bold]  [dim]{note}[/dim]")

    # `is False` only: a pending item is not a failure, and exiting non-zero
    # on "nobody has signed in yet" would fail a correct fresh install.
    failed = [c for c in checks if c[1] is False]
    todo = [c[3] for c in checks if c[1] is None and c[3]]
    console.print()
    if failed:
        console.print(f"[red]{len(failed)} check(s) need attention.[/red] See README.md → Setup.")
        raise typer.Exit(1)
    console.print("[green]Nothing broken.[/green]")
    for step in todo:
        console.print(f"[dim]Next: {step}[/dim]")
    if not todo:
        console.print("[dim]Start the chat UI with: oncallbot serve[/dim]")


@app.command()
def adopt(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    email: Annotated[str, typer.Option(help="The address the existing token authorizes.")] = "",
) -> None:
    """Move a pre-login token and summary cache to their per-user paths.

    Login gives every person their own token file and their own store. Run this
    once if you already had `oncallbot auth` working, so you neither re-consent
    nor start with an empty cache.
    """
    import shutil

    cfg = _load(config)
    addr = (email or cfg.gmail.account or "").strip().lower()
    if not addr:
        err.print("[red]Give --email, or set gmail.account in config.yaml.[/red]")
        raise typer.Exit(2)

    if not cfg.gmail.token_file.exists():
        err.print(f"[red]No token at {cfg.gmail.token_file} to adopt.[/red]")
        raise typer.Exit(2)

    # Verify before copying: a token adopted under the wrong address would read
    # one mailbox while claiming to be another.
    from .gmail_auth import authorized_email, build_service

    actual = authorized_email(
        build_service(cfg.gmail.credentials_file, cfg.gmail.token_file)
    ).lower()
    if actual != addr:
        err.print(
            f"[red]{cfg.gmail.token_file} authorizes {actual}, not {addr}.[/red]\n"
            f"Run with --email {actual}, or delete the token and sign in fresh."
        )
        raise typer.Exit(2)

    target = cfg.for_user(addr)
    target.gmail.token_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cfg.gmail.token_file, target.gmail.token_file)
    target.gmail.token_file.chmod(0o600)
    console.print(f"[green]token[/green]  {cfg.gmail.token_file} → {target.gmail.token_file}")

    if cfg.store_path.exists() and not target.store_path.exists():
        target.store_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg.store_path, target.store_path)
        with Store(target.store_path) as store:
            n = store.total()
        console.print(f"[green]store[/green]  {n} summary(ies) → {target.store_path}")
    elif target.store_path.exists():
        console.print(f"[dim]store already exists at {target.store_path}, left alone[/dim]")

    console.print(f"\n{addr} can now sign in without re-consenting.")


@app.command()
def serve(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    host: Annotated[str, typer.Option(help="Bind address. Keep it on loopback.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = DEFAULT_PORT,
) -> None:
    """Serve the chat UI. Google sign-in per user -- see docs/deploy.md."""
    import uvicorn

    cfg = _load(config)  # fail fast on a bad config rather than on the first request
    from .chat.server import create_app

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback:
        # A session cookie over plain HTTP on a shared network is sniffable,
        # and it authorizes reading someone's mailbox. This is now a refusal,
        # not a warning: the cookie is what changed.
        err.print(
            f"[red]Refusing to bind {host} over plain HTTP.[/red]\n"
            "The session cookie authorizes Gmail access; on anything but "
            "loopback it needs HTTPS in front, plus the identity and audit "
            "items in docs/deploy.md. Put a TLS terminator ahead of a loopback "
            "bind, or set auth.enabled: false to run single-user as before."
        )
        raise typer.Exit(code=2)

    pidfile = process.pid_path(cfg.store_path)

    # Checked before anything is printed, because uvicorn's "address already in
    # use" is a traceback rather than an instruction -- and because writing our
    # pid over the running server's entry would leave it unfindable once we
    # exit. Announcing the URL first and then refusing reads as a crash.
    running = process.find_server(pidfile, port)
    if running is not None:
        err.print(
            f"[yellow]Already running on port {port} (pid {running.pid}).[/yellow]\n"
            "Use `oncallbot restart` to replace it, or `oncallbot stop` first."
        )
        raise typer.Exit(code=1)

    # Google redirects the browser back here, so the URL must be the one the
    # user's browser can reach -- and the one registered on the OAuth client.
    public_url = f"http://{'localhost' if host in ('127.0.0.1', 'localhost') else host}:{port}"
    console.print(f"[green]oncallbot chat[/green] → {public_url}")
    if cfg.auth.enabled:
        console.print(
            f"[dim]Sign in with Google in the browser. Redirect URI: "
            f"{public_url}{cfg.auth.redirect_path}[/dim]"
        )
    # Recorded so `stop` and `restart` can find this process without guessing
    # from the port -- and removed on the way out, whether that is Ctrl-C, a
    # SIGTERM from `stop`, or a crash.
    process.write_pidfile(pidfile, port)
    try:
        uvicorn.run(
            create_app(config, public_url=public_url),
            host=host, port=port, log_level="warning",
        )
    finally:
        # Only our own entry: a newer server may already have claimed the file.
        process.clear_pidfile(pidfile, os.getpid())


@app.command()
def stop(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    port: Annotated[int, typer.Option(help="Port the server is on.")] = DEFAULT_PORT,
    force: Annotated[bool, typer.Option(help="SIGKILL instead of asking nicely.")] = False,
) -> None:
    """Stop the running chat server."""
    cfg = _load(config)
    server = process.find_server(process.pid_path(cfg.store_path), port)

    if server is None:
        holder = process.port_holder(port)
        if holder is None:
            console.print(f"[dim]Nothing is running on port {port}.[/dim]")
            return
        # Something is there, but it is not ours. Say so rather than signalling
        # it: on this machine 8080 is nginx.
        pid, command = holder
        err.print(
            f"[yellow]Port {port} is held by pid {pid}, which is not an "
            f"oncallbot server:[/yellow]\n  {escape(command[:160])}\n"
            "Left alone. Stop it yourself, or pass --port for the right one."
        )
        raise typer.Exit(code=1)

    console.print(f"Stopping pid {server.pid} on port {server.port} …")
    try:
        outcome = process.stop(server, force=force)
    except ProcessLookupError:
        console.print("[dim]It had already exited.[/dim]")
        process.clear_pidfile(process.pid_path(cfg.store_path), server.pid)
        return
    except PermissionError:
        err.print(f"[red]Not allowed to signal pid {server.pid}.[/red]")
        raise typer.Exit(code=1) from None

    process.clear_pidfile(process.pid_path(cfg.store_path), server.pid)
    if outcome == "stuck":
        err.print(f"[red]pid {server.pid} ignored both signals.[/red]")
        raise typer.Exit(code=1)
    if outcome == "killed":
        console.print("[green]Killed.[/green]")
    else:
        console.print("[green]Stopped.[/green]")


@app.command()
def restart(
    config: ConfigOpt = DEFAULT_CONFIG_PATH,
    host: Annotated[str, typer.Option(help="Bind address. Keep it on loopback.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = DEFAULT_PORT,
) -> None:
    """Stop the running chat server, then start it again in the foreground."""
    cfg = _load(config)
    server = process.find_server(process.pid_path(cfg.store_path), port)
    if server is None:
        console.print("[dim]Nothing to stop; starting.[/dim]")
    else:
        console.print(f"Stopping pid {server.pid} …")
        try:
            outcome = process.stop(server)
        except ProcessLookupError:
            outcome = "stopped"
        except PermissionError:
            err.print(f"[red]Not allowed to signal pid {server.pid}.[/red]")
            raise typer.Exit(code=1) from None
        if outcome == "stuck":
            # Starting now would just fail to bind, with a worse message.
            err.print(f"[red]pid {server.pid} would not stop, so not starting.[/red]")
            raise typer.Exit(code=1)
        process.clear_pidfile(process.pid_path(cfg.store_path), server.pid)

    serve(config=config, host=host, port=port)


if __name__ == "__main__":
    app()
