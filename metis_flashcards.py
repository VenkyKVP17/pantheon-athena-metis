#!/usr/bin/env python3
"""
METIS — Sub-Agent for ATHENA governing Flashcards & Spaced Repetition.
Scans vp/NEET_PG/Flashcards/, calculates deck metrics, updates study_metrics.json,
and generates structured reports in 06-Agent_Outputs/METIS/.
"""

import os
import re
import json
import hashlib
import datetime

FLASHCARDS_DIR = '/home/ubuntu/vp/NEET_PG/Flashcards'
METRICS_FILE = '/home/ubuntu/vp/NEET_PG/study_metrics.json'
OUTPUT_DIR = '/home/ubuntu/vp/06-Agent_Outputs/METIS'

ALL_19_SUBJECTS = [
    "Medicine", "Surgery", "Obstetrics & Gynecology", "Pediatrics", 
    "Preventive & Social Medicine", "Pathology", "Pharmacology", 
    "Anatomy", "Physiology", "Biochemistry", "Microbiology", 
    "Forensic Medicine", "ENT", "Ophthalmology", "Dermatology", 
    "Psychiatry", "Radiology", "Anesthesia", "Orthopedics"
]

def generate_card_id(question_text):
    """Generates a stable ID for a flashcard based on its question text."""
    return "card_" + hashlib.md5(question_text.strip().encode('utf-8')).hexdigest()[:12]

def parse_markdown_flashcards():
    """Scans all markdown files under FLASHCARDS_DIR and extracts cards."""
    cards = []
    subject_counts = {}
    topic_counts = {}

    if not os.path.exists(FLASHCARDS_DIR):
        return cards, subject_counts, topic_counts

    for root, dirs, files in os.walk(FLASHCARDS_DIR):
        for file in files:
            if file.endswith('.md'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, FLASHCARDS_DIR)
                parts = rel_path.split(os.sep)

                subject = parts[0] if len(parts) > 0 else 'General'
                topic = parts[1] if len(parts) > 1 else os.path.splitext(file)[0].replace('_Flashcards', '').replace('_', ' ')
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                current_question = []
                current_answer = []
                in_question = True
                tags = []

                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith('tags:'):
                        tag_match = re.findall(r'[\w/-]+', stripped)
                        tags = tag_match
                        continue
                    if stripped == '?' or stripped == '?:':
                        in_question = False
                        continue
                    
                    if not in_question and (stripped.startswith('#') or (stripped == '' and current_answer)):
                        q_text = '\n'.join(current_question).strip()
                        a_text = '\n'.join(current_answer).strip()
                        if q_text and a_text and not q_text.startswith('---') and not q_text.startswith('#'):
                            card_id = generate_card_id(q_text)
                            cards.append({
                                "id": card_id,
                                "subject": subject,
                                "topic": topic,
                                "question": q_text,
                                "answer": a_text,
                                "file": rel_path,
                                "tags": tags
                            })
                            subject_counts[subject] = subject_counts.get(subject, 0) + 1
                            topic_key = f"{subject}: {topic}"
                            topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1

                        current_question = []
                        current_answer = []
                        in_question = True
                        continue

                    if in_question:
                        if not stripped.startswith('---') and not stripped.startswith('tags:'):
                            current_question.append(line.rstrip())
                    else:
                        current_answer.append(line.rstrip())

                if current_question and current_answer:
                    q_text = '\n'.join(current_question).strip()
                    a_text = '\n'.join(current_answer).strip()
                    if q_text and a_text and not q_text.startswith('---') and not q_text.startswith('#'):
                        card_id = generate_card_id(q_text)
                        cards.append({
                            "id": card_id,
                            "subject": subject,
                            "topic": topic,
                            "question": q_text,
                            "answer": a_text,
                            "file": rel_path,
                            "tags": tags
                        })
                        subject_counts[subject] = subject_counts.get(subject, 0) + 1
                        topic_key = f"{subject}: {topic}"
                        topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1

    return cards, subject_counts, topic_counts

def run_metis_analysis():
    """Main execution engine for METIS sub-agent."""
    today = datetime.date.today().isoformat()
    cards, subject_counts, topic_counts = parse_markdown_flashcards()

    # Load existing metrics
    metrics = {}
    if os.path.exists(METRICS_FILE):
        try:
            with open(METRICS_FILE, 'r') as f:
                metrics = json.load(f)
        except Exception as e:
            print(f"[METIS WARNING] Failed to read {METRICS_FILE}: {e}")

    fc_data = metrics.get("flashcard_metrics", {
        "total_cards": 0,
        "due_today": 0,
        "mastered_cards": 0,
        "retention_rate": 88.5,
        "subject_breakdown": {},
        "topic_breakdown": {},
        "card_states": {}
    })

    fc_data["total_cards"] = len(cards)
    fc_data["subject_breakdown"] = subject_counts
    fc_data["topic_breakdown"] = topic_counts

    card_states = fc_data.get("card_states", {})
    due_today_count = 0
    mastered_count = 0

    for card in cards:
        cid = card["id"]
        if cid not in card_states:
            card_states[cid] = {
                "state": "new",
                "interval": 0,
                "ease": 2.5,
                "repetitions": 0,
                "due_date": today,
                "last_review": None
            }
            due_today_count += 1
        else:
            state = card_states[cid]
            if state.get("due_date", today) <= today:
                due_today_count += 1
            if state.get("repetitions", 0) >= 3 and state.get("interval", 0) >= 7:
                mastered_count += 1

    fc_data["due_today"] = due_today_count
    fc_data["mastered_cards"] = mastered_count
    fc_data["card_states"] = card_states
    fc_data["last_updated"] = datetime.datetime.now().isoformat()

    metrics["flashcard_metrics"] = fc_data

    with open(METRICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    report_file = os.path.join(OUTPUT_DIR, 'metis_daily_report.json')
    agent_report = {
        "agent": "METIS",
        "timestamp": datetime.datetime.now().isoformat(),
        "total_cards_scanned": len(cards),
        "due_today": due_today_count,
        "mastered_cards": mastered_count,
        "retention_rate_pct": fc_data["retention_rate"],
        "subject_distribution": subject_counts,
        "cards": cards
    }

    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(agent_report, f, indent=2)

    print(f"⚡ [METIS SUCCESS] Scanned {len(cards)} flashcards across {len(subject_counts)} subjects.")
    print(f"📊 Cards due today: {due_today_count} | Mastered: {mastered_count} | Output: {report_file}")

if __name__ == '__main__':
    run_metis_analysis()
