#!/usr/bin/env python3
"""
METIS DB — SQLite-backed multi-user Anki-style flashcard store.
Auth (username/password), decks, cards, per-user FSRS scheduling state,
and review history. This replaces the old localStorage-only progress
tracking so reviews survive across devices/browsers and are separated
per user.
"""
import sqlite3
import hashlib
import hmac
import secrets
import datetime
import json
import os

import fsrs

import metis_sm2

DB_FILE = '/home/ubuntu/vp/NEET_PG/metis.db'

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    UNIQUE(subject, topic)
);

CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    deck_id INTEGER NOT NULL,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    source_file TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(deck_id) REFERENCES decks(id)
);

CREATE TABLE IF NOT EXISTS card_state (
    user_id INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    state INTEGER NOT NULL,
    step INTEGER,
    stability REAL,
    difficulty REAL,
    due TEXT NOT NULL,
    last_review TEXT,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, card_id),
    FOREIGN KEY(card_id) REFERENCES cards(id)
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    rating INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    review_duration_ms INTEGER,
    prev_state_json TEXT,
    FOREIGN KEY(card_id) REFERENCES cards(id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    algorithm TEXT NOT NULL DEFAULT 'fsrs',
    desired_retention REAL NOT NULL DEFAULT 0.9,
    learning_steps_min TEXT NOT NULL DEFAULT '1,10',
    relearning_steps_min TEXT NOT NULL DEFAULT '10',
    maximum_interval_days INTEGER NOT NULL DEFAULT 36500,
    enable_fuzzing INTEGER NOT NULL DEFAULT 1,
    new_cards_per_day INTEGER NOT NULL DEFAULT 20,
    max_reviews_per_day INTEGER NOT NULL DEFAULT 200,
    graduating_interval_days INTEGER NOT NULL DEFAULT 1,
    easy_interval_days INTEGER NOT NULL DEFAULT 4,
    starting_ease REAL NOT NULL DEFAULT 2.5,
    fsrs_parameters TEXT,
    fsrs_optimized_at TEXT,
    fsrs_optimized_review_count INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS card_archive (
    user_id INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (user_id, card_id),
    FOREIGN KEY(card_id) REFERENCES cards(id)
);

CREATE TABLE IF NOT EXISTS card_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    flag_type TEXT NOT NULL,
    related_card_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(card_id) REFERENCES cards(id)
);

CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id);
CREATE INDEX IF NOT EXISTS idx_review_log_user_time ON review_log(user_id, reviewed_at);
CREATE INDEX IF NOT EXISTS idx_card_flags_status ON card_flags(status);
"""

DEFAULT_FSRS_PARAMETERS = list(fsrs.Scheduler().parameters)

# ---------------------------------------------------------- Migration ----
# card_state predates per-algorithm scheduling; these columns are added
# on top of any existing production DB so upgrades don't lose progress.
_CARD_STATE_MIGRATIONS = [
    ("algorithm", "TEXT NOT NULL DEFAULT 'fsrs'"),
    ("first_seen_at", "TEXT"),
]

# The card auditor (metis_card_auditor.py) uses this to only fact-check cards
# it hasn't already judged; reset to NULL on edit so a changed card gets
# re-checked on the next nightly pass.
_CARDS_MIGRATIONS = [
    ("audited_at", "TEXT"),
]


def _migrate(conn):
    existing_cols = {row['name'] for row in conn.execute('PRAGMA table_info(card_state)').fetchall()}
    for col, decl in _CARD_STATE_MIGRATIONS:
        if col not in existing_cols:
            conn.execute(f'ALTER TABLE card_state ADD COLUMN {col} {decl}')

    cards_cols = {row['name'] for row in conn.execute('PRAGMA table_info(cards)').fetchall()}
    for col, decl in _CARDS_MIGRATIONS:
        if col not in cards_cols:
            conn.execute(f'ALTER TABLE cards ADD COLUMN {col} {decl}')

    conn.commit()


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    conn.close()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- Auth ----

def hash_password(password, salt_hex=None):
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), 200000)
    return h.hex(), salt_hex


def create_user(username, password):
    username = username.strip()
    if not username or not password or len(password) < 4:
        raise ValueError('Username required and password must be at least 4 characters.')
    conn = get_conn()
    try:
        pw_hash, salt = hash_password(password)
        conn.execute(
            'INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)',
            (username, pw_hash, salt, now_iso())
        )
        conn.commit()
        return conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()['id']
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_conn()
    try:
        row = conn.execute('SELECT id, password_hash, salt FROM users WHERE username = ?', (username.strip(),)).fetchone()
        if not row:
            return None
        computed, _ = hash_password(password, row['salt'])
        if hmac.compare_digest(computed, row['password_hash']):
            return row['id']
        return None
    finally:
        conn.close()


def create_session(user_id, days=30):
    conn = get_conn()
    try:
        token = secrets.token_hex(32)
        expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()
        conn.execute(
            'INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)',
            (token, user_id, now_iso(), expires)
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_user_from_session(token):
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT s.user_id AS id, s.expires_at AS expires_at, u.username AS username '
            'FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?',
            (token,)
        ).fetchone()
        if not row or row['expires_at'] < now_iso():
            return None
        return {'id': row['id'], 'username': row['username']}
    finally:
        conn.close()


def delete_session(token):
    conn = get_conn()
    conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
    conn.commit()
    conn.close()


# --------------------------------------------------------- Decks/Cards ----

def get_or_create_deck(subject, topic):
    conn = get_conn()
    try:
        row = conn.execute('SELECT id FROM decks WHERE subject = ? AND topic = ?', (subject, topic)).fetchone()
        if row:
            return row['id']
        cur = conn.execute('INSERT INTO decks (subject, topic) VALUES (?, ?)', (subject, topic))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def upsert_card_from_import(card_id, deck_id, front, back, tags, source_file):
    """Used by the markdown migration: insert if new, leave untouched if it
    already exists (so re-running the import never clobbers edits made from
    the app, and never resets anyone's review progress)."""
    conn = get_conn()
    try:
        existing = conn.execute('SELECT id FROM cards WHERE id = ?', (card_id,)).fetchone()
        if existing:
            return False
        conn.execute(
            'INSERT INTO cards (id, deck_id, front, back, tags, source_file, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (card_id, deck_id, front, back, json.dumps(tags), source_file, now_iso(), now_iso())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def create_card(subject, topic, front, back, tags):
    deck_id = get_or_create_deck(subject, topic)
    card_id = 'card_' + secrets.token_hex(8)
    conn = get_conn()
    try:
        conn.execute(
            'INSERT INTO cards (id, deck_id, front, back, tags, source_file, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            (card_id, deck_id, front, back, json.dumps(tags), now_iso(), now_iso())
        )
        conn.commit()
        return card_id
    finally:
        conn.close()


def update_card(card_id, front=None, back=None, tags=None):
    conn = get_conn()
    try:
        row = conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
        if not row:
            return False
        # A content edit invalidates any prior auditor fact-check, so it gets
        # re-queued for the next nightly pass.
        content_changed = (front is not None and front != row['front']) or (back is not None and back != row['back'])
        conn.execute(
            'UPDATE cards SET front = ?, back = ?, tags = ?, updated_at = ?, audited_at = ? WHERE id = ?',
            (
                front if front is not None else row['front'],
                back if back is not None else row['back'],
                json.dumps(tags) if tags is not None else row['tags'],
                now_iso(),
                None if content_changed else row['audited_at'],
                card_id
            )
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_card(card_id):
    conn = get_conn()
    conn.execute('UPDATE cards SET deleted = 1, updated_at = ? WHERE id = ?', (now_iso(), card_id))
    conn.commit()
    conn.close()


def archive_card(user_id, card_id):
    """Per-user 'never show me this again' — unlike delete_card, doesn't
    touch the shared card so other users' decks are unaffected."""
    conn = get_conn()
    conn.execute(
        'INSERT OR IGNORE INTO card_archive (user_id, card_id, archived_at) VALUES (?, ?, ?)',
        (user_id, card_id, now_iso())
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------- Card flags ----
# Written only by metis_card_auditor.py (nightly duplicate + fact-check
# pass). Never auto-deletes/edits cards -- everything lands here as 'open'
# for a human to review in the dashboard's Review Queue.

def create_card_flag(card_id, flag_type, reason, related_card_id=None):
    """Insert a flag unless an identical one (any status) already exists, so
    a dismissed flag doesn't get silently re-raised on the next nightly scan.
    For 'duplicate' flags, matches regardless of which card is A vs B."""
    conn = get_conn()
    try:
        if related_card_id:
            existing = conn.execute(
                'SELECT id FROM card_flags WHERE flag_type = ? AND '
                '((card_id = ? AND related_card_id = ?) OR (card_id = ? AND related_card_id = ?))',
                (flag_type, card_id, related_card_id, related_card_id, card_id)
            ).fetchone()
        else:
            existing = conn.execute(
                'SELECT id FROM card_flags WHERE flag_type = ? AND card_id = ? AND related_card_id IS NULL',
                (flag_type, card_id)
            ).fetchone()
        if existing:
            return False
        conn.execute(
            'INSERT INTO card_flags (card_id, flag_type, related_card_id, reason, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (card_id, flag_type, related_card_id, reason, 'open', now_iso())
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_flags(status='open'):
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT f.id, f.card_id, f.flag_type, f.related_card_id, f.reason, f.status, f.created_at, '
            '       c.front AS card_front, c.back AS card_back, d.subject AS subject, d.topic AS topic, '
            '       rc.front AS related_front '
            'FROM card_flags f '
            'JOIN cards c ON c.id = f.card_id '
            'JOIN decks d ON d.id = c.deck_id '
            'LEFT JOIN cards rc ON rc.id = f.related_card_id '
            'WHERE f.status = ? ORDER BY f.created_at DESC',
            (status,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_flag_status(flag_id, status):
    conn = get_conn()
    cur = conn.execute(
        'UPDATE card_flags SET status = ?, resolved_at = ? WHERE id = ?',
        (status, now_iso(), flag_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def get_unaudited_cards(limit=500):
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT id, front, back FROM cards WHERE deleted = 0 AND audited_at IS NULL LIMIT ?',
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_cards_audited(card_ids):
    if not card_ids:
        return
    conn = get_conn()
    ts = now_iso()
    conn.executemany(
        'UPDATE cards SET audited_at = ? WHERE id = ?',
        [(ts, cid) for cid in card_ids]
    )
    conn.commit()
    conn.close()


def get_active_cards_by_deck():
    """{(subject, topic): [{'id', 'front'}, ...]} for all non-deleted cards --
    used by the duplicate scan, which only makes sense within a deck."""
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT c.id, c.front, d.subject, d.topic FROM cards c '
            'JOIN decks d ON d.id = c.deck_id WHERE c.deleted = 0 '
            'ORDER BY d.subject, d.topic'
        ).fetchall()
        by_deck = {}
        for r in rows:
            key = (r['subject'], r['topic'])
            by_deck.setdefault(key, []).append({'id': r['id'], 'front': r['front']})
        return by_deck
    finally:
        conn.close()


def list_decks_with_counts(user_id):
    algorithm = get_user_settings(user_id)['algorithm']
    conn = get_conn()
    try:
        decks = conn.execute('SELECT id, subject, topic FROM decks ORDER BY subject, topic').fetchall()
        out = []
        now = now_iso()
        for d in decks:
            total = conn.execute(
                'SELECT COUNT(*) c FROM cards WHERE deck_id = ? AND deleted = 0', (d['id'],)
            ).fetchone()['c']
            if total == 0:
                continue
            new_count = conn.execute(
                'SELECT COUNT(*) c FROM cards ca LEFT JOIN card_state cs '
                'ON cs.card_id = ca.id AND cs.user_id = ? '
                'WHERE ca.deck_id = ? AND ca.deleted = 0 AND (cs.card_id IS NULL OR cs.algorithm != ?)',
                (user_id, d['id'], algorithm)
            ).fetchone()['c']
            due_count = conn.execute(
                'SELECT COUNT(*) c FROM cards ca JOIN card_state cs '
                'ON cs.card_id = ca.id AND cs.user_id = ? '
                'WHERE ca.deck_id = ? AND ca.deleted = 0 AND cs.algorithm = ? AND cs.due <= ?',
                (user_id, d['id'], algorithm, now)
            ).fetchone()['c']
            out.append({
                'id': d['id'], 'subject': d['subject'], 'topic': d['topic'],
                'total': total, 'new': new_count, 'due': due_count + new_count
            })
        return out
    finally:
        conn.close()


def list_cards(user_id, deck_id=None, search=None, tag=None, limit=200, offset=0):
    conn = get_conn()
    try:
        query = (
            'SELECT ca.id, ca.front, ca.back, ca.tags, ca.deck_id, d.subject, d.topic, '
            'cs.state, cs.due, cs.reps, cs.lapses '
            'FROM cards ca JOIN decks d ON d.id = ca.deck_id '
            'LEFT JOIN card_state cs ON cs.card_id = ca.id AND cs.user_id = ? '
            'WHERE ca.deleted = 0'
        )
        params = [user_id]
        if deck_id:
            query += ' AND ca.deck_id = ?'
            params.append(deck_id)
        if search:
            query += ' AND (ca.front LIKE ? OR ca.back LIKE ?)'
            like = f'%{search}%'
            params += [like, like]
        if tag:
            query += ' AND ca.tags LIKE ?'
            params.append(f'%"{tag}"%')
        query += ' ORDER BY d.subject, d.topic, ca.created_at LIMIT ? OFFSET ?'
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['tags'] = json.loads(d['tags']) if d['tags'] else []
            out.append(d)
        return out
    finally:
        conn.close()


def get_card(card_id):
    conn = get_conn()
    try:
        row = conn.execute('SELECT * FROM cards WHERE id = ? AND deleted = 0', (card_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------- Settings ----

def _row_to_settings_dict(row):
    return {
        'algorithm': row['algorithm'],
        'desired_retention': row['desired_retention'],
        'learning_steps_min': row['learning_steps_min'],
        'relearning_steps_min': row['relearning_steps_min'],
        'maximum_interval_days': row['maximum_interval_days'],
        'enable_fuzzing': bool(row['enable_fuzzing']),
        'new_cards_per_day': row['new_cards_per_day'],
        'max_reviews_per_day': row['max_reviews_per_day'],
        'graduating_interval_days': row['graduating_interval_days'],
        'easy_interval_days': row['easy_interval_days'],
        'starting_ease': row['starting_ease'],
        'fsrs_parameters': json.loads(row['fsrs_parameters']) if row['fsrs_parameters'] else None,
        'fsrs_optimized_at': row['fsrs_optimized_at'],
        'fsrs_optimized_review_count': row['fsrs_optimized_review_count'],
    }


def get_user_settings(user_id):
    conn = get_conn()
    try:
        row = conn.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,)).fetchone()
        if not row:
            conn.execute('INSERT INTO user_settings (user_id, updated_at) VALUES (?, ?)', (user_id, now_iso()))
            conn.commit()
            row = conn.execute('SELECT * FROM user_settings WHERE user_id = ?', (user_id,)).fetchone()
        return _row_to_settings_dict(row)
    finally:
        conn.close()


def _validate_steps(steps_str):
    steps_str = (steps_str or '').strip()
    parts = [p.strip() for p in steps_str.split(',') if p.strip()]
    if not parts:
        raise ValueError('Steps cannot be empty (e.g. "1,10")')
    for p in parts:
        if not p.isdigit() or not (1 <= int(p) <= 43200):
            raise ValueError('Each step must be a whole number of minutes between 1 and 43200 (30 days)')
    return ','.join(parts)


def update_user_settings(user_id, updates):
    """updates: dict of any subset of the user_settings columns. Raises ValueError on bad input."""
    get_user_settings(user_id)  # ensures a row exists
    set_clauses, params = [], []

    def add(col, value):
        set_clauses.append(f'{col} = ?')
        params.append(value)

    if 'algorithm' in updates:
        algo = updates['algorithm']
        if algo not in ('fsrs', 'sm2'):
            raise ValueError("algorithm must be 'fsrs' or 'sm2'")
        add('algorithm', algo)

    if 'desired_retention' in updates:
        v = float(updates['desired_retention'])
        if not (0.70 <= v <= 0.99):
            raise ValueError('desired_retention must be between 0.70 and 0.99')
        add('desired_retention', v)

    if 'learning_steps_min' in updates:
        add('learning_steps_min', _validate_steps(updates['learning_steps_min']))

    if 'relearning_steps_min' in updates:
        add('relearning_steps_min', _validate_steps(updates['relearning_steps_min']))

    if 'maximum_interval_days' in updates:
        v = int(updates['maximum_interval_days'])
        if not (1 <= v <= 36500):
            raise ValueError('maximum_interval_days must be between 1 and 36500')
        add('maximum_interval_days', v)

    if 'enable_fuzzing' in updates:
        add('enable_fuzzing', 1 if updates['enable_fuzzing'] else 0)

    if 'new_cards_per_day' in updates:
        v = int(updates['new_cards_per_day'])
        if not (0 <= v <= 9999):
            raise ValueError('new_cards_per_day must be between 0 and 9999')
        add('new_cards_per_day', v)

    if 'max_reviews_per_day' in updates:
        v = int(updates['max_reviews_per_day'])
        if not (0 <= v <= 99999):
            raise ValueError('max_reviews_per_day must be between 0 and 99999')
        add('max_reviews_per_day', v)

    if 'graduating_interval_days' in updates:
        v = int(updates['graduating_interval_days'])
        if not (1 <= v <= 3650):
            raise ValueError('graduating_interval_days must be between 1 and 3650')
        add('graduating_interval_days', v)

    if 'easy_interval_days' in updates:
        v = int(updates['easy_interval_days'])
        if not (1 <= v <= 3650):
            raise ValueError('easy_interval_days must be between 1 and 3650')
        add('easy_interval_days', v)

    if 'starting_ease' in updates:
        v = float(updates['starting_ease'])
        if not (1.3 <= v <= 5.0):
            raise ValueError('starting_ease must be between 1.3 and 5.0')
        add('starting_ease', v)

    if not set_clauses:
        return get_user_settings(user_id)

    add('updated_at', now_iso())
    params.append(user_id)
    conn = get_conn()
    try:
        conn.execute(f'UPDATE user_settings SET {", ".join(set_clauses)} WHERE user_id = ?', params)
        conn.commit()
    finally:
        conn.close()
    return get_user_settings(user_id)


def today_str():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


# ------------------------------------------------------------- FSRS ----

def _build_fsrs_scheduler(settings):
    """Builds an fsrs.Scheduler from a user's settings dict, using their
    personalized weights if optimize_user_parameters() has been run,
    otherwise the library's stock FSRS-6 defaults."""
    kwargs = {
        'parameters': settings['fsrs_parameters'] or DEFAULT_FSRS_PARAMETERS,
        'desired_retention': settings['desired_retention'],
        'maximum_interval': settings['maximum_interval_days'],
        'enable_fuzzing': settings['enable_fuzzing'],
    }
    learning_steps = metis_sm2.parse_minutes(settings['learning_steps_min'])
    relearning_steps = metis_sm2.parse_minutes(settings['relearning_steps_min'])
    if learning_steps:
        kwargs['learning_steps'] = [datetime.timedelta(minutes=m) for m in learning_steps]
    if relearning_steps:
        kwargs['relearning_steps'] = [datetime.timedelta(minutes=m) for m in relearning_steps]
    return fsrs.Scheduler(**kwargs)


def _row_to_fsrs_card(row):
    if row is None:
        return fsrs.Card()
    return fsrs.Card.from_dict({
        'card_id': 0,
        'state': row['state'],
        'step': row['step'],
        'stability': row['stability'],
        'difficulty': row['difficulty'],
        'due': row['due'],
        'last_review': row['last_review'],
    })


RATING_NAME_TO_INT = {'again': 1, 'hard': 2, 'good': 3, 'easy': 4}


def _schedule_review(state_row, rating_int, settings):
    """Returns (new_state_dict, lapse: bool) for whichever algorithm the
    user currently has active. If state_row's stored algorithm doesn't
    match (i.e. the user switched schedulers since this card was last
    reviewed), its progress is stale under the new algorithm's semantics
    and the card is treated as brand new — same as Anki resetting cards
    on a scheduler version change."""
    effective_row = None
    if state_row is not None and state_row['algorithm'] == settings['algorithm']:
        effective_row = state_row

    if settings['algorithm'] == 'sm2':
        row_dict = dict(effective_row) if effective_row is not None else None
        return metis_sm2.review(row_dict, rating_int, settings)

    scheduler = _build_fsrs_scheduler(settings)
    card = _row_to_fsrs_card(effective_row)
    was_review = effective_row is not None and effective_row['state'] == fsrs.State.Review.value
    rating = fsrs.Rating(rating_int)
    updated, _log = scheduler.review_card(card, rating)
    lapse = was_review and rating == fsrs.Rating.Again
    d = updated.to_dict()
    new_state = {
        'state': d['state'], 'step': d['step'], 'stability': d['stability'],
        'difficulty': d['difficulty'], 'due': d['due'], 'last_review': d['last_review'],
    }
    return new_state, lapse


def _persist_card_state(conn, user_id, card_id, new_state, algorithm, reps_delta=0, lapse=False):
    existing = conn.execute(
        'SELECT reps, lapses, algorithm, first_seen_at FROM card_state WHERE user_id = ? AND card_id = ?',
        (user_id, card_id)
    ).fetchone()
    fresh = existing is None or existing['algorithm'] != algorithm
    reps = (0 if fresh else existing['reps']) + reps_delta
    lapses = (0 if fresh else existing['lapses']) + (1 if lapse else 0)
    first_seen_at = now_iso() if fresh else existing['first_seen_at']
    conn.execute(
        'INSERT INTO card_state (user_id, card_id, state, step, stability, difficulty, due, last_review, '
        'reps, lapses, algorithm, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(user_id, card_id) DO UPDATE SET '
        'state=excluded.state, step=excluded.step, stability=excluded.stability, difficulty=excluded.difficulty, '
        'due=excluded.due, last_review=excluded.last_review, reps=excluded.reps, lapses=excluded.lapses, '
        'algorithm=excluded.algorithm, first_seen_at=excluded.first_seen_at',
        (user_id, card_id, new_state['state'], new_state['step'], new_state['stability'], new_state['difficulty'],
         new_state['due'], new_state['last_review'], reps, lapses, algorithm, first_seen_at)
    )


def get_due_queue(user_id, deck_id=None, limit=20, new_limit=10, due_only=False):
    settings = get_user_settings(user_id)
    algorithm = settings['algorithm']
    conn = get_conn()
    try:
        now = now_iso()
        today = today_str()

        new_shown_today = conn.execute(
            'SELECT COUNT(*) c FROM card_state WHERE user_id = ? AND algorithm = ? AND substr(first_seen_at, 1, 10) = ?',
            (user_id, algorithm, today)
        ).fetchone()['c']
        reviews_done_today = conn.execute(
            'SELECT COUNT(*) c FROM review_log WHERE user_id = ? AND substr(reviewed_at, 1, 10) = ?',
            (user_id, today)
        ).fetchone()['c']

        new_allowance = max(settings['new_cards_per_day'] - new_shown_today, 0)
        review_allowance = max(settings['max_reviews_per_day'] - reviews_done_today, 0)

        base = (
            'FROM cards ca JOIN decks d ON d.id = ca.deck_id '
            'LEFT JOIN card_state cs ON cs.card_id = ca.id AND cs.user_id = ? '
            'WHERE ca.deleted = 0 '
            'AND ca.id NOT IN (SELECT card_id FROM card_archive WHERE user_id = ?)'
        )
        params = [user_id, user_id]
        if deck_id:
            base += ' AND ca.deck_id = ?'
            params.append(deck_id)

        due_limit = min(limit, review_allowance)
        due_rows = []
        if due_limit > 0:
            due_rows = conn.execute(
                f'SELECT ca.id, ca.front, ca.back, ca.tags, d.subject, d.topic, cs.state, cs.due '
                f'{base} AND cs.card_id IS NOT NULL AND cs.algorithm = ? AND cs.due <= ? '
                f'ORDER BY cs.state ASC, cs.due ASC LIMIT ?',
                params + [algorithm, now, due_limit]
            ).fetchall()

        # New cards have their own daily allowance, independent of the
        # review cap — hitting one cap shouldn't block the other queue.
        remaining_batch = max(limit - len(due_rows), 0)
        new_slot = 0 if due_only else min(new_limit, new_allowance, remaining_batch)
        new_rows = []
        if new_slot > 0:
            new_rows = conn.execute(
                f'SELECT ca.id, ca.front, ca.back, ca.tags, d.subject, d.topic, NULL as state, NULL as due '
                f'{base} AND (cs.card_id IS NULL OR cs.algorithm != ?) '
                f'ORDER BY ca.created_at LIMIT ?',
                params + [algorithm, new_slot]
            ).fetchall()

        out = []
        for r in list(due_rows) + list(new_rows):
            d = dict(r)
            d['tags'] = json.loads(d['tags']) if d['tags'] else []
            out.append(d)

        return {
            'cards': out,
            'caps': {
                'new_shown_today': new_shown_today, 'new_cards_per_day': settings['new_cards_per_day'],
                'new_cap_reached': new_allowance <= 0,
                'reviews_done_today': reviews_done_today, 'max_reviews_per_day': settings['max_reviews_per_day'],
                'review_cap_reached': review_allowance <= 0,
            }
        }
    finally:
        conn.close()


def humanize_interval(due, from_iso=None):
    if isinstance(due, str):
        due = datetime.datetime.fromisoformat(due)
    frm = datetime.datetime.fromisoformat(from_iso) if from_iso else datetime.datetime.now(datetime.timezone.utc)
    delta = due - frm
    seconds = delta.total_seconds()
    if seconds < 3600:
        return f'{max(round(seconds / 60), 1)}m'
    if seconds < 86400:
        return f'{round(seconds / 3600)}h'
    days = seconds / 86400
    if days < 30:
        return f'{round(days)}d'
    if days < 365:
        return f'{round(days / 30, 1)}mo'
    return f'{round(days / 365, 1)}y'


def preview_intervals(user_id, card_id):
    settings = get_user_settings(user_id)
    conn = get_conn()
    try:
        state_row = conn.execute(
            'SELECT * FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, card_id)
        ).fetchone()
    finally:
        conn.close()
    out = {}
    for name, rating_int in RATING_NAME_TO_INT.items():
        new_state, _lapse = _schedule_review(state_row, rating_int, settings)
        out[name] = humanize_interval(new_state['due'])
    return out


def apply_review(user_id, card_id, rating_name, duration_ms=None):
    rating_int = RATING_NAME_TO_INT.get(rating_name)
    if rating_int is None:
        raise ValueError(f'Unknown rating: {rating_name}')

    settings = get_user_settings(user_id)
    conn = get_conn()
    try:
        state_row = conn.execute(
            'SELECT * FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, card_id)
        ).fetchone()
        prev_state_json = json.dumps(dict(state_row)) if state_row else None

        new_state, lapse = _schedule_review(state_row, rating_int, settings)
        _persist_card_state(conn, user_id, card_id, new_state, settings['algorithm'], reps_delta=1, lapse=lapse)

        conn.execute(
            'INSERT INTO review_log (user_id, card_id, rating, reviewed_at, review_duration_ms, prev_state_json) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, card_id, rating_int, now_iso(), duration_ms, prev_state_json)
        )
        conn.commit()
        return {'due': new_state['due'], 'state': new_state['state'],
                'interval': humanize_interval(new_state['due'])}
    finally:
        conn.close()


def undo_last_review(user_id):
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT * FROM review_log WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,)
        ).fetchone()
        if not row:
            return False
        if row['prev_state_json']:
            prev = json.loads(row['prev_state_json'])
            conn.execute(
                'UPDATE card_state SET state=?, step=?, stability=?, difficulty=?, due=?, last_review=?, '
                'reps=?, lapses=?, algorithm=?, first_seen_at=? WHERE user_id=? AND card_id=?',
                (prev['state'], prev['step'], prev['stability'], prev['difficulty'], prev['due'],
                 prev['last_review'], prev['reps'], prev['lapses'], prev['algorithm'], prev['first_seen_at'],
                 user_id, row['card_id'])
            )
        else:
            conn.execute('DELETE FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, row['card_id']))
        conn.execute('DELETE FROM review_log WHERE id = ?', (row['id'],))
        conn.commit()
        return True
    finally:
        conn.close()


def get_stats(user_id):
    settings = get_user_settings(user_id)
    algorithm = settings['algorithm']
    conn = get_conn()
    try:
        total_cards = conn.execute('SELECT COUNT(*) c FROM cards WHERE deleted = 0').fetchone()['c']
        new_count = conn.execute(
            'SELECT COUNT(*) c FROM cards ca LEFT JOIN card_state cs ON cs.card_id = ca.id AND cs.user_id = ? '
            'WHERE ca.deleted = 0 AND (cs.card_id IS NULL OR cs.algorithm != ?)', (user_id, algorithm)
        ).fetchone()['c']
        by_state = conn.execute(
            'SELECT state, COUNT(*) c FROM card_state WHERE user_id = ? AND algorithm = ? GROUP BY state',
            (user_id, algorithm)
        ).fetchall()
        state_counts = {row['state']: row['c'] for row in by_state}
        mastered = conn.execute(
            'SELECT COUNT(*) c FROM card_state WHERE user_id = ? AND algorithm = ? AND state = ? AND stability >= 21',
            (user_id, algorithm, fsrs.State.Review.value)
        ).fetchone()['c']

        total_reviews = conn.execute('SELECT COUNT(*) c FROM review_log WHERE user_id = ?', (user_id,)).fetchone()['c']
        successful = conn.execute(
            'SELECT COUNT(*) c FROM review_log WHERE user_id = ? AND rating != ?',
            (user_id, RATING_NAME_TO_INT['again'])
        ).fetchone()['c']
        retention = round(successful / total_reviews * 100, 1) if total_reviews else None

        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        activity_rows = conn.execute(
            "SELECT substr(reviewed_at, 1, 10) day, COUNT(*) c FROM review_log "
            "WHERE user_id = ? AND reviewed_at >= ? GROUP BY day ORDER BY day",
            (user_id, since)
        ).fetchall()
        activity = {row['day']: row['c'] for row in activity_rows}

        today = datetime.date.today()
        forecast = {}
        for i in range(14):
            day = (today + datetime.timedelta(days=i)).isoformat()
            forecast[day] = conn.execute(
                'SELECT COUNT(*) c FROM card_state WHERE user_id = ? AND algorithm = ? AND substr(due, 1, 10) = ?',
                (user_id, algorithm, day)
            ).fetchone()['c']

        return {
            'total_cards': total_cards,
            'new_count': new_count,
            'learning_count': state_counts.get(fsrs.State.Learning.value, 0),
            'review_count': state_counts.get(fsrs.State.Review.value, 0),
            'relearning_count': state_counts.get(fsrs.State.Relearning.value, 0),
            'mastered_count': mastered,
            'retention_rate_pct': retention,
            'total_reviews': total_reviews,
            'activity_last_30_days': activity,
            'forecast_next_14_days': forecast,
            'algorithm': algorithm,
        }
    finally:
        conn.close()


# --------------------------------------------------------- Optimizer ----

# fsrs.Optimizer has its own internal floor: it counts only non-same-day
# reviews of cards already in the Review state (i.e. not their first
# couple of learning-step reviews), and if that count is below its
# mini_batch_size (512 as of py-fsrs 6.3.1) it silently returns the stock
# DEFAULT_PARAMETERS unchanged -- no exception, no warning. A raw
# review_log row count is a cheap pre-filter, not the real gate; the
# authoritative check is comparing the output against the stock defaults
# below, since that's the one signal the library actually gives us.
MIN_REVIEWS_FOR_OPTIMIZATION = 200


def optimize_user_parameters(user_id):
    """Fits personalized FSRS-6 weights from this user's own review
    history via fsrs.Optimizer (gradient descent, torch backend). This is
    the actual point of FSRS over SM-2 — generic weights fit an average
    student, personalized weights fit this one. Requires a substantial
    review history; below that the stock defaults stay in use."""
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT card_id, rating, reviewed_at, review_duration_ms FROM review_log '
            'WHERE user_id = ? ORDER BY reviewed_at ASC', (user_id,)
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < MIN_REVIEWS_FOR_OPTIMIZATION:
        raise ValueError(
            f'Need at least {MIN_REVIEWS_FOR_OPTIMIZATION} logged reviews before attempting '
            f'optimization (you have {len(rows)}). Keep studying — stock FSRS-6 weights are used until then.'
        )

    # fsrs.ReviewLog wants an int card_id; ours are opaque strings. The
    # mapping only needs to be internally consistent for this one run.
    card_id_map = {}
    review_logs = []
    for row in rows:
        cid = row['card_id']
        if cid not in card_id_map:
            card_id_map[cid] = len(card_id_map) + 1
        review_logs.append(fsrs.ReviewLog(
            card_id=card_id_map[cid],
            rating=fsrs.Rating(row['rating']),
            review_datetime=datetime.datetime.fromisoformat(row['reviewed_at']),
            review_duration=row['review_duration_ms'],
        ))

    optimizer = fsrs.Optimizer(review_logs)
    new_parameters = optimizer.compute_optimal_parameters()

    if list(new_parameters) == DEFAULT_FSRS_PARAMETERS:
        # The library's own 512-review-state-review floor wasn't met, so it
        # silently declined to optimize. Surface that honestly instead of
        # persisting a fake "personalized" result that's just the defaults.
        raise ValueError(
            'Not enough qualifying review history yet for FSRS to personalize your weights '
            '(it needs several hundred non-same-day reviews of cards already in the Review '
            'state, not just any reviews). Keep studying and try again later — stock FSRS-6 '
            'weights are being used in the meantime.'
        )

    conn = get_conn()
    try:
        conn.execute(
            'UPDATE user_settings SET fsrs_parameters = ?, fsrs_optimized_at = ?, '
            'fsrs_optimized_review_count = ?, updated_at = ? WHERE user_id = ?',
            (json.dumps(new_parameters), now_iso(), len(rows), now_iso(), user_id)
        )
        conn.commit()
    finally:
        conn.close()

    return {'parameters': new_parameters, 'review_count': len(rows)}


def reset_fsrs_parameters(user_id):
    """Drops back to stock FSRS-6 default weights."""
    conn = get_conn()
    try:
        conn.execute(
            'UPDATE user_settings SET fsrs_parameters = NULL, fsrs_optimized_at = NULL, '
            'fsrs_optimized_review_count = NULL, updated_at = ? WHERE user_id = ?',
            (now_iso(), user_id)
        )
        conn.commit()
    finally:
        conn.close()
