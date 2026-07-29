#!/usr/bin/env python3
"""
One-time (idempotent) import of Flashcards/*.md into the METIS SQLite DB.
Safe to re-run: existing cards (matched by stable content-hash id) are left
untouched so nobody's review progress or edits get clobbered; only genuinely
new cards from the markdown vault get inserted.
"""
import os
import re
import hashlib

import metis_db as db

FLASHCARDS_DIR = '/home/ubuntu/vp/NEET_PG/Flashcards'


def generate_card_id(question_text):
    return "card_" + hashlib.md5(question_text.strip().encode('utf-8')).hexdigest()[:12]


def parse_markdown_flashcards():
    cards = []
    if not os.path.exists(FLASHCARDS_DIR):
        return cards

    for root, _dirs, files in os.walk(FLASHCARDS_DIR):
        for file in files:
            if not file.endswith('.md'):
                continue
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, FLASHCARDS_DIR)
            parts = rel_path.split(os.sep)
            subject = parts[0] if len(parts) > 0 else 'General'
            # Flat layout is Subject/Topic_Flashcards.md (no topic subfolder) --
            # derive the topic from the filename so it lines up with the
            # Title_Case_With_Underscores taxonomy used by subject_topics_reference.json.
            # A nested Subject/Topic/file.md layout (if ever used) takes the folder name instead.
            if len(parts) >= 3:
                topic = parts[1]
            else:
                topic = os.path.splitext(file)[0].replace('_Flashcards', '')

            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            current_question, current_answer = [], []
            in_question = True
            tags = []

            def flush():
                q_text = '\n'.join(current_question).strip()
                a_text = '\n'.join(current_answer).strip()
                if q_text and a_text and not q_text.startswith('---') and not q_text.startswith('#'):
                    cards.append({
                        "id": generate_card_id(q_text),
                        "subject": subject, "topic": topic,
                        "front": q_text, "back": a_text,
                        "file": rel_path, "tags": list(tags),
                    })

            for line in lines:
                stripped = line.strip()
                if stripped.startswith('tags:'):
                    tags = re.findall(r'[\w/-]+', stripped.split(':', 1)[1])
                    continue
                if stripped in ('?', '?:'):
                    in_question = False
                    continue
                if not in_question and (stripped.startswith('#') or (stripped == '' and current_answer)):
                    flush()
                    current_question, current_answer = [], []
                    in_question = True
                    continue
                if in_question:
                    if not stripped.startswith('---') and not stripped.startswith('tags:'):
                        current_question.append(line.rstrip())
                else:
                    current_answer.append(line.rstrip())

            if current_question and current_answer:
                flush()

    return cards


def run():
    db.init_db()
    cards = parse_markdown_flashcards()
    inserted = 0
    for c in cards:
        deck_id = db.get_or_create_deck(c['subject'], c['topic'])
        if db.upsert_card_from_import(c['id'], deck_id, c['front'], c['back'], c['tags'], c['file']):
            inserted += 1
    print(f"METIS migrate: scanned {len(cards)} markdown cards, inserted {inserted} new, "
          f"{len(cards) - inserted} already present.")
    return len(cards), inserted


if __name__ == '__main__':
    run()
