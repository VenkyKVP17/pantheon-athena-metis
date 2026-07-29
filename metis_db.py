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

CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id);
CREATE INDEX IF NOT EXISTS idx_review_log_user_time ON review_log(user_id, reviewed_at);
"""

_scheduler = fsrs.Scheduler(desired_retention=0.9)


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
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
        conn.execute(
            'UPDATE cards SET front = ?, back = ?, tags = ?, updated_at = ? WHERE id = ?',
            (
                front if front is not None else row['front'],
                back if back is not None else row['back'],
                json.dumps(tags) if tags is not None else row['tags'],
                now_iso(),
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


def list_decks_with_counts(user_id):
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
                'WHERE ca.deck_id = ? AND ca.deleted = 0 AND cs.card_id IS NULL',
                (user_id, d['id'])
            ).fetchone()['c']
            due_count = conn.execute(
                'SELECT COUNT(*) c FROM cards ca JOIN card_state cs '
                'ON cs.card_id = ca.id AND cs.user_id = ? '
                'WHERE ca.deck_id = ? AND ca.deleted = 0 AND cs.due <= ?',
                (user_id, d['id'], now)
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


# ------------------------------------------------------------- FSRS ----

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


def _persist_card_state(conn, user_id, card_id, card, reps_delta=0, lapse=False):
    d = card.to_dict()
    existing = conn.execute(
        'SELECT reps, lapses FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, card_id)
    ).fetchone()
    reps = (existing['reps'] if existing else 0) + reps_delta
    lapses = (existing['lapses'] if existing else 0) + (1 if lapse else 0)
    conn.execute(
        'INSERT INTO card_state (user_id, card_id, state, step, stability, difficulty, due, last_review, reps, lapses) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(user_id, card_id) DO UPDATE SET '
        'state=excluded.state, step=excluded.step, stability=excluded.stability, difficulty=excluded.difficulty, '
        'due=excluded.due, last_review=excluded.last_review, reps=excluded.reps, lapses=excluded.lapses',
        (user_id, card_id, d['state'], d['step'], d['stability'], d['difficulty'], d['due'], d['last_review'], reps, lapses)
    )


def get_due_queue(user_id, deck_id=None, limit=20, new_limit=10):
    conn = get_conn()
    try:
        now = now_iso()
        base = (
            'FROM cards ca JOIN decks d ON d.id = ca.deck_id '
            'LEFT JOIN card_state cs ON cs.card_id = ca.id AND cs.user_id = ? '
            'WHERE ca.deleted = 0'
        )
        params = [user_id]
        if deck_id:
            base += ' AND ca.deck_id = ?'
            params.append(deck_id)

        due_rows = conn.execute(
            f'SELECT ca.id, ca.front, ca.back, ca.tags, d.subject, d.topic, cs.state, cs.due '
            f'{base} AND cs.card_id IS NOT NULL AND cs.due <= ? '
            f'ORDER BY cs.state ASC, cs.due ASC LIMIT ?',
            params + [now, limit]
        ).fetchall()

        remaining = max(limit - len(due_rows), 0)
        new_rows = []
        if remaining > 0:
            new_rows = conn.execute(
                f'SELECT ca.id, ca.front, ca.back, ca.tags, d.subject, d.topic, NULL as state, NULL as due '
                f'{base} AND cs.card_id IS NULL ORDER BY ca.created_at LIMIT ?',
                params + [min(remaining, new_limit)]
            ).fetchall()

        out = []
        for r in list(due_rows) + list(new_rows):
            d = dict(r)
            d['tags'] = json.loads(d['tags']) if d['tags'] else []
            out.append(d)
        return out
    finally:
        conn.close()


RATING_MAP = {'again': fsrs.Rating.Again, 'hard': fsrs.Rating.Hard, 'good': fsrs.Rating.Good, 'easy': fsrs.Rating.Easy}


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
    conn = get_conn()
    try:
        state_row = conn.execute(
            'SELECT * FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, card_id)
        ).fetchone()
    finally:
        conn.close()
    card = _row_to_fsrs_card(state_row)
    out = {}
    for name, rating in RATING_MAP.items():
        updated, _ = _scheduler.review_card(card, rating)
        out[name] = humanize_interval(updated.due)
    return out


def apply_review(user_id, card_id, rating_name, duration_ms=None):
    rating = RATING_MAP.get(rating_name)
    if rating is None:
        raise ValueError(f'Unknown rating: {rating_name}')

    conn = get_conn()
    try:
        state_row = conn.execute(
            'SELECT * FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, card_id)
        ).fetchone()
        card = _row_to_fsrs_card(state_row)
        was_review = state_row is not None and state_row['state'] == fsrs.State.Review.value
        prev_state_json = json.dumps(dict(state_row)) if state_row else None

        updated, log = _scheduler.review_card(card, rating)
        lapse = was_review and rating == fsrs.Rating.Again
        _persist_card_state(conn, user_id, card_id, updated, reps_delta=1, lapse=lapse)

        conn.execute(
            'INSERT INTO review_log (user_id, card_id, rating, reviewed_at, review_duration_ms, prev_state_json) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, card_id, rating.value, now_iso(), duration_ms, prev_state_json)
        )
        conn.commit()
        return {'due': updated.due.isoformat() if hasattr(updated.due, 'isoformat') else updated.due,
                'state': updated.state.value, 'interval': humanize_interval(
                    updated.due.isoformat() if hasattr(updated.due, 'isoformat') else updated.due)}
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
                'UPDATE card_state SET state=?, step=?, stability=?, difficulty=?, due=?, last_review=?, reps=?, lapses=? '
                'WHERE user_id=? AND card_id=?',
                (prev['state'], prev['step'], prev['stability'], prev['difficulty'], prev['due'],
                 prev['last_review'], prev['reps'], prev['lapses'], user_id, row['card_id'])
            )
        else:
            conn.execute('DELETE FROM card_state WHERE user_id = ? AND card_id = ?', (user_id, row['card_id']))
        conn.execute('DELETE FROM review_log WHERE id = ?', (row['id'],))
        conn.commit()
        return True
    finally:
        conn.close()


def get_stats(user_id):
    conn = get_conn()
    try:
        total_cards = conn.execute('SELECT COUNT(*) c FROM cards WHERE deleted = 0').fetchone()['c']
        new_count = conn.execute(
            'SELECT COUNT(*) c FROM cards ca LEFT JOIN card_state cs ON cs.card_id = ca.id AND cs.user_id = ? '
            'WHERE ca.deleted = 0 AND cs.card_id IS NULL', (user_id,)
        ).fetchone()['c']
        by_state = conn.execute(
            'SELECT state, COUNT(*) c FROM card_state WHERE user_id = ? GROUP BY state', (user_id,)
        ).fetchall()
        state_counts = {row['state']: row['c'] for row in by_state}
        mastered = conn.execute(
            'SELECT COUNT(*) c FROM card_state WHERE user_id = ? AND state = ? AND stability >= 21',
            (user_id, fsrs.State.Review.value)
        ).fetchone()['c']

        total_reviews = conn.execute('SELECT COUNT(*) c FROM review_log WHERE user_id = ?', (user_id,)).fetchone()['c']
        successful = conn.execute(
            'SELECT COUNT(*) c FROM review_log WHERE user_id = ? AND rating != ?',
            (user_id, fsrs.Rating.Again.value)
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
                'SELECT COUNT(*) c FROM card_state WHERE user_id = ? AND substr(due, 1, 10) = ?',
                (user_id, day)
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
        }
    finally:
        conn.close()
