"""Pure user-facing formatting for terminal agent turn outcomes.

The host loop still records mutation state, evaluates feature gates, finalizes
turns, and performs all persistence, request, model-call, cancellation, and
session-switching work.
"""

from __future__ import annotations

import re
from typing import Any


class TurnResultFormattingMixin:
    """Render bounded turn-result notices without mutating agent state."""

    _FOOTER_PATH_RE = re.compile(
        r"(?<![/:\w.`])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.[\w]+",
    )

    @classmethod
    def _neutralize_footer_paths(cls, text: str) -> str:
        """Wrap bare file paths in backticks so they aren't auto-delivered.

        The gateway's ``extract_local_files`` scans response text for bare
        absolute/home paths ending in a deliverable extension and uploads
        any that exist on disk as native attachments — but it explicitly
        skips paths inside inline-code (`` `...` ``) spans.  Backticking
        every path the footer renders defeats that auto-detection while
        keeping the path fully human-readable.  Paths already wrapped in a
        backtick (the negative lookbehind excludes a preceding `` ` ``) are
        left untouched so we never double-wrap.
        """
        if not text:
            return text
        return cls._FOOTER_PATH_RE.sub(lambda m: f"`{m.group(0)}`", text)

    @classmethod
    def _format_file_mutation_failure_footer(
        cls, failed: dict[str, dict[str, Any]]
    ) -> str:
        """Render the per-turn failed-mutation dict as a user-facing footer.

        Displays up to 10 paths with their first error preview, then a
        count of any additional failures.  Returns an empty string when
        the dict is empty so callers can concatenate unconditionally.

        Every file path that reaches the user-facing text — both the bullet
        path and any path echoed inside the tool's error preview — is
        backtick-wrapped via ``_neutralize_footer_paths`` so the gateway's
        bare-path media extractor can never auto-attach a protected file
        (e.g. the runtime ``config.yaml``) to a messaging channel (#35584).
        """
        if not failed:
            return ""
        lines = [
            (
                "⚠️ File-mutation verifier: "
                f"{len(failed)} file(s) were NOT modified this turn despite any "
                "wording above that may suggest otherwise. Run `git status` or "
                "`read_file` to confirm."
            )
        ]
        shown = 0
        for path, info in failed.items():
            if shown >= 10:
                break
            preview = (info.get("error_preview") or "").strip()
            tool = info.get("tool") or "patch"
            if preview:
                lines.append(f"  • `{path}` — [{tool}] {preview}")
            else:
                lines.append(f"  • `{path}` — [{tool}] failed")
            shown += 1
        remaining = len(failed) - shown
        if remaining > 0:
            lines.append(f"  • … and {remaining} more")
        # Neutralize any path the preview text echoed (the bullet path is
        # already backticked above; the lookbehind keeps it from being
        # double-wrapped).
        return cls._neutralize_footer_paths("\n".join(lines))

    @staticmethod
    def _format_turn_completion_explanation(
        turn_exit_reason: str, persistence_cause: str | None = None
    ) -> str:
        """Render a user-facing explanation for an abnormal turn ending.

        Maps the internal ``turn_exit_reason`` to a short, actionable
        message so a turn that produced no usable assistant reply (empty
        content after retries, a partial/truncated stream, a still-pending
        tool result, or an iteration/budget limit) is never silent from
        the UI's perspective — the symptom users report in #34452.

        ``persistence_cause`` refines the ``session_persistence_failed``
        wording (see ``classify_persistence_error``): lock contention gets
        "storage was busy, send it again" instead of the disk-space advice,
        which was a misdiagnosis for that failure mode. It is optional and
        ignored for every other reason, so one-argument callers keep the
        exact behavior they had before.

        Returns an empty string for reasons that are NOT abnormal (e.g.
        a normal ``text_response(...)`` exit), so callers can concatenate
        or substitute unconditionally without warning on healthy turns
        like a terse ``Done.``.
        """
        if not turn_exit_reason:
            return ""
        reason = str(turn_exit_reason)

        # Normal completion — stay quiet.  ``text_response(...)`` is the
        # healthy terminal; anything that produced a real reply is fine.
        if reason.startswith("text_response"):
            return ""

        prefix = "⚠️ No reply: "
        if reason == "empty_response_exhausted":
            return (
                prefix + "the model returned empty content after retries and any "
                "fallback providers. Try `continue`, switch model/provider, "
                "or inspect the tool output above."
            )
        if reason == "all_retries_exhausted_no_response":
            return (
                prefix + "all API retries were exhausted before a response was "
                "produced (provider errors / rate limits). Try `continue` "
                "or switch provider."
            )
        if reason == "partial_stream_recovery":
            return (
                prefix + "streaming stopped early and only a partial response was "
                "recovered. Send `continue` to resume from where it stopped."
            )
        if reason == "fallback_prior_turn_content":
            return (
                prefix + "no new content was produced this turn; showing recovered "
                "prior context. Send `continue` to retry."
            )
        if reason == "interrupted_during_api_call":
            return (
                prefix + "the request was interrupted mid-call before a reply was "
                "received. Send `continue` to retry."
            )
        if reason == "budget_exhausted":
            return (
                prefix + "the per-turn iteration/cost budget was exhausted before a "
                "final answer. Send `continue` to keep going."
            )
        if reason == "ollama_runtime_context_too_small":
            return (
                prefix + "the local model's context window was too small to finish. "
                "Increase the context size or use a larger model."
            )
        if reason.startswith("max_iterations_reached"):
            return (
                prefix + "the maximum tool-iteration limit was reached before a "
                "final answer. Send `continue` to keep going, or raise "
                "`max_iterations`."
            )
        if reason.startswith("error_near_max_iterations"):
            return (
                prefix + "an error occurred near the iteration limit before a final "
                "answer. Check the tool output above, then send `continue`."
            )
        if reason == "pending_tool_result":
            return (
                prefix + "the turn stopped while a tool result was still pending and "
                "the model produced no follow-up text. Send `continue` to "
                "let it summarize."
            )
        if reason == "session_persistence_failed":
            cause = persistence_cause or "unknown"
            if cause == "compression":
                return (
                    prefix + "the turn was stopped because another process was "
                    "compressing this session. Your message should already be "
                    "saved — please send it again after compression completes."
                )
            if cause == "compression_closed":
                return (
                    prefix + "the turn was stopped because this session was rotated "
                    "by context compression and its live continuation could "
                    "not be adopted. The storage itself is healthy — refresh "
                    "the client (or start a new turn) so it picks up the new "
                    "session id, then send your message again."
                )
            if cause == "turn_lease":
                return (
                    prefix + "the turn was stopped because another PCBDraft process "
                    "took over this session. Your reply was not saved — wait "
                    "for the other process to finish, then send your message "
                    "again."
                )
            if cause == "locked":
                return (
                    prefix + "the turn was stopped because session storage was busy "
                    "(another PCBDraft process was writing to the state "
                    "database). Your message should already be saved — "
                    "please send it again in a moment."
                )
            if cause == "corrupt":
                return (
                    prefix + "the turn was stopped because the state database "
                    "reported structural corruption (the transcript would "
                    "have been lost on restart). Freeing disk space will "
                    "not help. Recovery options:\n"
                    "1. Run `pcbdraft doctor`\n"
                    '2. Salvage with sqlite3 on the runtime state.db using ".recover" '
                    "(then replace state.db)\n"
                    "3. Restore from a backup in <PCBDRAFT_RUNTIME_HOME>/backups/\n"
                    "Then send your message again."
                )
            if cause == "disk":
                return (
                    prefix + "the turn was stopped because session storage could not "
                    "be written (the transcript would have been lost on "
                    "restart). This is often a full disk — free some space "
                    "(or fix state.db permissions), then send your message "
                    "again."
                )
            return (
                prefix + "the turn was stopped because session storage could not be "
                "written (the transcript would have been lost on restart). "
                "Check the state database health (`pcbdraft doctor`), then "
                "send your message again."
            )
        # Unknown/diagnostic-only reasons (e.g. "unknown", guardrail_halt
        # which already surfaces its own message) — don't second-guess.
        return ""
