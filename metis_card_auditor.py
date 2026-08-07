#!/usr/bin/env python3
"""
METIS Card Auditor -- nightly pass over the flashcard deck:
  1. Fact-check: any card never audited before is sent to agy for a
     medical-accuracy sanity check; flagged if the answer looks wrong,
     outdated, or doubtful.
  2. Duplicate scan: within each deck (subject+topic), asks agy to spot
     cards that test the exact same underlying fact even if worded
     differently -- not just verbatim text matches.

Never edits or deletes a card itself. Every finding lands in the
card_flags table as 'open' for a human to review in the dashboard's
Review Queue (see /api/metis/flags in dropzone_server.py).
"""
import json
import subprocess
import sys

sys.path.insert(0, "/home/ubuntu/vp/.scripts")
from nyx_notify import notify, notify_error  # noqa: E402

sys.path.insert(0, "/home/ubuntu/pantheon-athena-metis")
import metis_db as db  # noqa: E402

AGY_BIN = "/home/ubuntu/.local/bin/agy"
FACT_CHECK_BATCH = 8
# Caps how many batches one run will fact-check. Keeps a steady-state night
# (a handful of new cards) instant, and drains a large backlog (e.g. the
# first run against the existing 221-card deck) gradually over several
# nights instead of firing ~28 agy calls in one go.
MAX_BATCHES_PER_RUN = 6


def run_agy(prompt, timeout=200):
    result = subprocess.run(
        [AGY_BIN, "--dangerously-skip-permissions", "--print-timeout", "3m", "--print", prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agy exited {result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


def extract_json(text):
    """agy sometimes wraps JSON in prose or a code fence -- pull out the
    first balanced {...} or [...] block instead of assuming a clean reply."""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("No JSON found in agy output")
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Unbalanced JSON in agy output")


def fact_check_new_cards():
    cards = db.get_unaudited_cards(limit=FACT_CHECK_BATCH * MAX_BATCHES_PER_RUN)
    flagged = 0
    for i in range(0, len(cards), FACT_CHECK_BATCH):
        batch = cards[i:i + FACT_CHECK_BATCH]
        prompt_lines = [
            "You are reviewing NEET-PG medical flashcards for factual accuracy. "
            "For each card below, judge whether the answer is medically correct "
            "against current standard references (Harrison's, Robbins, Bailey & "
            "Love, current NEET-PG consensus).",
            'Respond with ONLY a JSON array, e.g. '
            '[{"id": "card_xxx", "verdict": "doubtful", "reason": "..."}]. '
            'Only include cards that are "doubtful" or "incorrect" -- omit cards '
            'that are correct. Empty array if all correct. No prose outside the JSON.',
            "",
        ]
        for c in batch:
            prompt_lines.append(f"id: {c['id']}\nQ: {c['front']}\nA: {c['back']}\n")

        try:
            issues = extract_json(run_agy("\n".join(prompt_lines)))
        except Exception as e:
            notify_error("METIS-AUDITOR", "Fact-check batch failed, will retry next run.", detail=str(e))
            continue  # leave this batch un-audited so it's retried next time

        batch_ids = {c["id"] for c in batch}
        for issue in issues:
            cid = issue.get("id")
            if cid not in batch_ids:
                continue
            reason = f"[{issue.get('verdict', 'doubtful')}] {issue.get('reason', '')}".strip()
            if db.create_card_flag(cid, "inaccurate", reason):
                flagged += 1
        db.mark_cards_audited(list(batch_ids))
    return flagged


def duplicate_scan():
    decks = db.get_active_cards_by_deck()
    flagged = 0
    for (subject, topic), cards in decks.items():
        if len(cards) < 2:
            continue
        prompt_lines = [
            f"These flashcards are all from the same NEET-PG study deck ({subject} / {topic}). "
            "Some may test the exact same underlying fact even if worded differently -- "
            "true duplicates, which waste spaced-repetition review time. Identify only "
            "genuine duplicate PAIRS (literally the same tested fact, not just related topic).",
            'Respond with ONLY a JSON array, e.g. '
            '[{"id_a": "card_x", "id_b": "card_y", "reason": "..."}]. Empty array if none.',
            "",
        ]
        for c in cards:
            prompt_lines.append(f"id: {c['id']}\nQ: {c['front']}")

        try:
            pairs = extract_json(run_agy("\n".join(prompt_lines)))
        except Exception as e:
            notify_error("METIS-AUDITOR", f"Duplicate scan failed for deck {subject}/{topic}.", detail=str(e))
            continue

        deck_ids = {c["id"] for c in cards}
        for pair in pairs:
            a, b = pair.get("id_a"), pair.get("id_b")
            if not a or not b or a not in deck_ids or b not in deck_ids or a == b:
                continue
            if db.create_card_flag(a, "duplicate", pair.get("reason", ""), related_card_id=b):
                flagged += 1
    return flagged


def main():
    db.init_db()
    fact_flagged = fact_check_new_cards()
    dup_flagged = duplicate_scan()
    total = fact_flagged + dup_flagged
    summary = f"{fact_flagged} accuracy flag(s), {dup_flagged} duplicate flag(s)"
    if total:
        notify("METIS-AUDITOR", f"Nightly card audit: {summary} added to the review queue.")
    print(f"METIS auditor: {summary}.")


if __name__ == "__main__":
    main()
