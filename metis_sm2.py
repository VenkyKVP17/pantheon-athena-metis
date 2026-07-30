#!/usr/bin/env python3
"""
METIS SM2 — classic Anki-style SM-2 scheduler.

py-fsrs only implements FSRS; it has no SM-2 mode. This module is a
from-scratch, faithful-but-simplified reimplementation of Anki's SM-2
scheduler (ease factor + learning/relearning steps + graduating/easy
intervals), so users can toggle between FSRS and "Anki SM-2 Standard"
in Settings.

State is represented as plain dicts matching the shape of the
`card_state` SQL row, so metis_db.py can persist FSRS and SM2 progress
through the same columns:
    state       -> 1 Learning / 2 Review / 3 Relearning (None = New)
    step        -> index into learning_steps / relearning_steps
    stability   -> repurposed as: current interval, in days
    difficulty  -> repurposed as: current ease factor (e.g. 2.5 = 250%)
    due         -> ISO datetime string
    last_review -> ISO datetime string
    reps        -> total successful reviews (tracked by the caller)
    lapses      -> total Again-from-Review lapses (tracked by the caller)
"""
from datetime import datetime, timedelta, timezone

STATE_LEARNING = 1
STATE_REVIEW = 2
STATE_RELEARNING = 3

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4

MIN_EASE = 1.3
HARD_EASE_PENALTY = 0.15
AGAIN_EASE_PENALTY = 0.20
EASY_EASE_BONUS = 0.15
EASY_INTERVAL_BONUS = 1.3
HARD_INTERVAL_FACTOR = 1.2


def parse_minutes(steps_str):
    """'1,10' -> [1, 10] (minutes). Empty/blank -> []."""
    if not steps_str:
        return []
    return [int(x.strip()) for x in steps_str.split(',') if x.strip()]


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def default_state():
    return {
        'state': None, 'step': None, 'stability': None, 'difficulty': None,
        'due': None, 'last_review': None,
    }


def review(state_row, rating, settings, now=None):
    """
    state_row: dict shaped like a card_state row, or None for a brand-new card.
    rating: one of AGAIN/HARD/GOOD/EASY (ints, fsrs.Rating-compatible).
    settings: dict with learning_steps_min, relearning_steps_min (comma strings),
              graduating_interval_days, easy_interval_days, starting_ease,
              maximum_interval_days.
    Returns: (new_state_dict, lapse: bool)
    """
    now = now or _now()
    learning_steps = parse_minutes(settings['learning_steps_min']) or [1, 10]
    relearning_steps = parse_minutes(settings['relearning_steps_min']) or [10]
    max_days = max(1, int(settings['maximum_interval_days']))
    grad_days = max(1, int(settings['graduating_interval_days']))
    easy_days = max(grad_days, int(settings['easy_interval_days']))
    starting_ease = max(MIN_EASE, float(settings['starting_ease']))

    lapse = False

    if state_row is None or state_row.get('state') is None:
        # ---- New card ----
        if rating == AGAIN:
            new = {'state': STATE_LEARNING, 'step': 0, 'stability': None,
                   'difficulty': starting_ease,
                   'due': _iso(now + timedelta(minutes=learning_steps[0]))}
        elif rating == HARD:
            new = {'state': STATE_LEARNING, 'step': 0, 'stability': None,
                   'difficulty': starting_ease,
                   'due': _iso(now + timedelta(minutes=learning_steps[0]))}
        elif rating == EASY:
            interval = min(easy_days, max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': interval,
                   'difficulty': starting_ease, 'due': _iso(now + timedelta(days=interval))}
        else:  # GOOD
            if len(learning_steps) <= 1:
                interval = min(grad_days, max_days)
                new = {'state': STATE_REVIEW, 'step': None, 'stability': interval,
                       'difficulty': starting_ease, 'due': _iso(now + timedelta(days=interval))}
            else:
                new = {'state': STATE_LEARNING, 'step': 1, 'stability': None,
                       'difficulty': starting_ease,
                       'due': _iso(now + timedelta(minutes=learning_steps[1]))}
        new['last_review'] = _iso(now)
        return new, lapse

    state = state_row['state']
    ease = state_row.get('difficulty') or starting_ease

    if state == STATE_LEARNING:
        step = state_row.get('step') or 0
        if rating in (AGAIN, HARD):
            new = {'state': STATE_LEARNING, 'step': 0, 'stability': None, 'difficulty': ease,
                   'due': _iso(now + timedelta(minutes=learning_steps[0]))}
        elif rating == EASY:
            interval = min(easy_days, max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': interval, 'difficulty': ease,
                   'due': _iso(now + timedelta(days=interval))}
        else:  # GOOD
            next_step = step + 1
            if next_step >= len(learning_steps):
                interval = min(grad_days, max_days)
                new = {'state': STATE_REVIEW, 'step': None, 'stability': interval, 'difficulty': ease,
                       'due': _iso(now + timedelta(days=interval))}
            else:
                new = {'state': STATE_LEARNING, 'step': next_step, 'stability': None, 'difficulty': ease,
                       'due': _iso(now + timedelta(minutes=learning_steps[next_step]))}

    elif state == STATE_RELEARNING:
        step = state_row.get('step') or 0
        if rating == AGAIN:
            new = {'state': STATE_RELEARNING, 'step': 0, 'stability': state_row.get('stability'),
                   'difficulty': ease, 'due': _iso(now + timedelta(minutes=relearning_steps[0]))}
        elif rating == HARD:
            new = {'state': STATE_RELEARNING, 'step': step, 'stability': state_row.get('stability'),
                   'difficulty': ease, 'due': _iso(now + timedelta(minutes=relearning_steps[step]))}
        elif rating == EASY:
            interval = min(max(grad_days, 1), max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': interval, 'difficulty': ease,
                   'due': _iso(now + timedelta(days=interval))}
        else:  # GOOD
            next_step = step + 1
            if next_step >= len(relearning_steps):
                interval = min(max(grad_days, 1), max_days)
                new = {'state': STATE_REVIEW, 'step': None, 'stability': interval, 'difficulty': ease,
                       'due': _iso(now + timedelta(days=interval))}
            else:
                new = {'state': STATE_RELEARNING, 'step': next_step, 'stability': state_row.get('stability'),
                       'difficulty': ease, 'due': _iso(now + timedelta(minutes=relearning_steps[next_step]))}

    else:  # STATE_REVIEW
        interval = state_row.get('stability') or grad_days
        if rating == AGAIN:
            lapse = True
            new_ease = max(MIN_EASE, ease - AGAIN_EASE_PENALTY)
            new = {'state': STATE_RELEARNING, 'step': 0, 'stability': interval, 'difficulty': new_ease,
                   'due': _iso(now + timedelta(minutes=relearning_steps[0]))}
        elif rating == HARD:
            new_ease = max(MIN_EASE, ease - HARD_EASE_PENALTY)
            new_interval = min(max(interval * HARD_INTERVAL_FACTOR, interval + 1), max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': new_interval, 'difficulty': new_ease,
                   'due': _iso(now + timedelta(days=new_interval))}
        elif rating == EASY:
            new_ease = ease + EASY_EASE_BONUS
            new_interval = min(interval * new_ease * EASY_INTERVAL_BONUS, max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': new_interval, 'difficulty': new_ease,
                   'due': _iso(now + timedelta(days=new_interval))}
        else:  # GOOD
            new_interval = min(interval * ease, max_days)
            new = {'state': STATE_REVIEW, 'step': None, 'stability': new_interval, 'difficulty': ease,
                   'due': _iso(now + timedelta(days=new_interval))}

    new['last_review'] = _iso(now)
    return new, lapse
