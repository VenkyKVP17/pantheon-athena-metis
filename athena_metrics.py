#!/usr/bin/env python3
import os
import re
import json
import datetime
import glob
import sqlite3

from question_classifier import classify_question, TYPE_LABELS

QBANK_DIR = '/home/ubuntu/vp/NEET_PG/QBank'
DROPZONE_DIR = '/home/ubuntu/vp/NEET_PG/Dropzone'
METRICS_FILE = '/home/ubuntu/vp/NEET_PG/study_metrics.json'
REFERENCE_FILE = '/home/ubuntu/vp/NEET_PG/subject_topics_reference.json'
BANK_TOTALS_FILE = '/home/ubuntu/vp/NEET_PG/cerebellum_bank_totals.json'

ALL_19_SUBJECTS = [
    "Medicine", "Surgery", "Obstetrics & Gynecology", "Pediatrics", 
    "Preventive & Social Medicine", "Pathology", "Pharmacology", 
    "Anatomy", "Physiology", "Biochemistry", "Microbiology", 
    "Forensic Medicine", "ENT", "Ophthalmology", "Dermatology", 
    "Psychiatry", "Radiology", "Anesthesia", "Orthopedics"
]

def scan_qbank_detailed():
    """Scans all QBank files for subject and topic level counts."""
    subject_counts = {}
    topic_counts = {}
    topic_mastery = {}
    total_questions = 0
    
    if not os.path.exists(QBANK_DIR):
        return total_questions, subject_counts, topic_counts, topic_mastery

    for root, dirs, files in os.walk(QBANK_DIR):
        for file in files:
            if file.endswith('.md'):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, QBANK_DIR)
                parts = rel_path.split(os.sep)
                
                subject = parts[0] if len(parts) > 0 else 'General'
                topic = parts[1] if len(parts) > 1 else os.path.splitext(file)[0].replace('QBank_', '').replace('_', ' ')
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    matches = re.findall(r'^## Q\d+', content, re.MULTILINE)
                    count = len(matches)
                    total_questions += count
                    
                    subject_counts[subject] = subject_counts.get(subject, 0) + count
                    topic_key = f"{subject}: {topic}"
                    topic_counts[topic_key] = topic_counts.get(topic_key, 0) + count
                    
                    # Local QBank file depth vs a 50-Q/subtopic rotation floor (needed for
                    # FSRS/SM-2 to have enough unique cards to avoid same-day repeats).
                    # This is NOT subject mastery or exam-bank coverage -- real qbanks like
                    # Cerebellum run ~15-20k+ Qs per subject, dwarfing this local floor.
                    depth_pct = min(round((count / 50.0) * 100, 1), 100.0)
                    topic_mastery[topic_key] = {
                        "count": count,
                        "depth_pct": depth_pct,
                        "status": "High Volume" if count >= 40 else ("Building Volume" if count >= 15 else "Starting Out")
                    }

    return total_questions, subject_counts, topic_counts, topic_mastery

def scan_qbank_question_types():
    """Classifies every QBank question stem by archetype (Except/Negative,
    Multi-Statement Evaluation, Clinical Vignette, Simple/Direct Recall) plus
    an image-based flag. This is a distribution over the question BANK
    (what you're exposed to), not accuracy -- QBank stores canonical Q&A,
    not your attempt history. Accuracy-by-type comes from Dropzone companions."""
    type_counts = {t: 0 for t in TYPE_LABELS}
    type_by_subject = {}
    image_based_count = 0
    total = 0

    if not os.path.exists(QBANK_DIR):
        return {"total": 0, "by_type": type_counts, "by_subject_type": {}, "image_based_count": 0}

    for path in glob.glob(os.path.join(QBANK_DIR, '**', '*.md'), recursive=True):
        rel_path = os.path.relpath(path, QBANK_DIR)
        subject = rel_path.split(os.sep)[0]
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        for m in re.finditer(r'\*\*Question:\*\*\s*(.+)', content):
            stem = m.group(1).strip()
            qtype, is_image = classify_question(stem)
            type_counts[qtype] = type_counts.get(qtype, 0) + 1
            type_by_subject.setdefault(subject, {t: 0 for t in TYPE_LABELS})
            type_by_subject[subject][qtype] += 1
            total += 1
            if is_image:
                image_based_count += 1

    return {
        "total": total,
        "by_type": type_counts,
        "by_type_labels": TYPE_LABELS,
        "by_subject_type": type_by_subject,
        "image_based_count": image_based_count,
    }

def scan_dropzone_companions():
    """Reads companion .json sidecars written alongside each Dropzone
    screenshot (timestamp of completion + question_type/correct once tagged).
    Powers real accuracy-by-question-type once screenshots get tagged --
    this is the "which type is actually costing me marks" signal, distinct
    from the QBank distribution above."""
    result = {
        "total_logged": 0,
        "tagged_count": 0,
        "accuracy_by_type": {},
        "completions": [],
    }
    if not os.path.exists(DROPZONE_DIR):
        return result

    type_totals = {}
    for path in sorted(glob.glob(os.path.join(DROPZONE_DIR, '*.meta.json'))):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                comp = json.load(f)
        except Exception:
            continue
        # Dropzone also receives unrelated screenshot-monitoring companions
        # from other tooling -- only count our own question-completion ones.
        if comp.get('companion_type') != 'qbank_completion':
            continue
        result["total_logged"] += 1
        result["completions"].append({
            "file": comp.get("screenshot_file"),
            "completed_at": comp.get("completed_at"),
            "question_type": comp.get("question_type"),
            "correct": comp.get("correct"),
        })
        qtype = comp.get("question_type")
        correct = comp.get("correct")
        if qtype and correct is not None:
            result["tagged_count"] += 1
            bucket = type_totals.setdefault(qtype, {"attempts": 0, "correct": 0})
            bucket["attempts"] += 1
            if correct:
                bucket["correct"] += 1

    for qtype, b in type_totals.items():
        result["accuracy_by_type"][qtype] = {
            "attempts": b["attempts"],
            "correct": b["correct"],
            "accuracy_pct": round(b["correct"] / b["attempts"] * 100, 1) if b["attempts"] else None,
        }

    return result

def load_reference():
    if not os.path.exists(REFERENCE_FILE):
        return {}
    with open(REFERENCE_FILE, 'r') as f:
        ref = json.load(f)
    ref.pop("_meta", None)
    return ref

def load_bank_totals():
    """Real per-subject question-bank totals (e.g. Cerebellum), sourced from
    GoFullPage screenshots parsed by the gofullpage webhook -> gemini-service
    pipeline. {} until at least one such screenshot has been ingested -- until
    then compute_syllabus_insights() falls back to the old 50-Q/topic floor
    estimate and flags it as such."""
    if not os.path.exists(BANK_TOTALS_FILE):
        return {}
    with open(BANK_TOTALS_FILE, 'r') as f:
        totals = json.load(f)
    totals.pop("_meta", None)
    return totals

def get_fsrs_metrics_by_subject():
    """Queries metis.db for average FSRS stability and difficulty per subject."""
    metrics = {}
    db_path = '/home/ubuntu/pantheon-athena-metis/metis.db'
    if not os.path.exists(db_path):
        return metrics
        
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Get stats for the primary user 'venky'
        query = '''
        SELECT d.subject, AVG(cs.stability) as avg_stability, AVG(cs.difficulty) as avg_difficulty,
               COUNT(cs.card_id) as card_count
        FROM card_state cs
        JOIN cards c ON cs.card_id = c.id
        JOIN decks d ON c.deck_id = d.id
        JOIN users u ON cs.user_id = u.id
        WHERE u.username = 'venky'
        GROUP BY d.subject;
        '''
        for row in conn.execute(query):
            metrics[row['subject']] = {
                'avg_stability': row['avg_stability'] or 0.0,
                'avg_difficulty': row['avg_difficulty'] or 5.0,
                'card_count': row['card_count']
            }
        conn.close()
    except Exception as e:
        print(f"Error reading FSRS metrics: {e}")
    return metrics

def compute_syllabus_insights(subject_counts, topic_counts, reference, bank_totals=None):
    """Algorithmic insights: Coverage %, Retention Mastery, and Predictive Yield."""
    subject_insights = {}
    fsrs_metrics = get_fsrs_metrics_by_subject()
    bank_totals = bank_totals or {}
    
    # Target stability for "Mastered" before the exam
    TARGET_STABILITY = 21.0 
    
    for subject in ALL_19_SUBJECTS:
        ref = reference.get(subject, {})
        canonical_topics = ref.get("topics", [])
        weightage_pct = ref.get("weightage_pct_approx", 0.0)

        attempted = set()
        for topic_key, count in topic_counts.items():
            if topic_key.startswith(subject + ": ") and count > 0:
                attempted.add(topic_key.split(": ", 1)[1])

        canonical_set = set(canonical_topics)
        matched = attempted & canonical_set
        unmatched = attempted - canonical_set

        real_total = bank_totals.get(subject)
        questions_solved = subject_counts.get(subject, 0)
        if real_total:
            # Real denominator from an actual question-bank total (e.g. Cerebellum),
            # parsed from a screenshot -- this is genuine subject coverage, not a guess.
            coverage_pct = round(min(questions_solved / real_total * 100.0, 100.0), 2)
            coverage_basis = "real_bank_total"
        else:
            # No real total known yet for this subject: fall back to the old
            # placeholder heuristic (count vs an arbitrary 50-Q/topic floor).
            # This number is NOT subject mastery or real coverage -- it's a stand-in
            # until a real total gets ingested via the gofullpage bank-volume pipeline.
            total_coverage = 0.0
            for topic in canonical_topics:
                topic_key = f"{subject}: {topic}"
                count = topic_counts.get(topic_key, 0)
                total_coverage += min((count / 50.0) * 100.0, 100.0)
            coverage_pct = round(total_coverage / len(canonical_topics), 1) if canonical_topics else 0.0
            coverage_basis = "estimated_floor"

        # Pull FSRS data
        subj_fsrs = fsrs_metrics.get(subject, {'avg_stability': 0.0, 'avg_difficulty': 5.0, 'card_count': 0})
        avg_stability = subj_fsrs['avg_stability']
        avg_difficulty = subj_fsrs['avg_difficulty']
        
        # Algorithmic Mastery (Coverage combined with Retention)
        # If stability is 0, we assume base retention of 10% just from short-term memory if coverage is high
        retention_factor = min(avg_stability / TARGET_STABILITY, 1.0)
        
        # User requested 60/40 split favoring retention of learned material over new material
        mastery_pct = (0.4 * coverage_pct) + (0.6 * coverage_pct * retention_factor)
        
        # Difficulty Multiplier: harder subjects need higher priority
        # Average difficulty in FSRS is around 5. 1-10 scale.
        difficulty_multiplier = 1.0 + (avg_difficulty - 5.0) / 10.0
        
        # Dynamic Priority Score
        priority_score = round((1 - mastery_pct / 100.0) * weightage_pct * difficulty_multiplier, 2)
        
        # Predictive Yield vs Effort (Marks gained per unit of study effort)
        # Yield = (Weightage - (Mastery/100 * Weightage))
        # Effort = max(1, avg_difficulty)
        estimated_yield_marks = round(weightage_pct - (mastery_pct / 100.0 * weightage_pct), 2)
        yield_effort_ratio = round(estimated_yield_marks / max(1.0, avg_difficulty), 2)

        subject_insights[subject] = {
            "weightage_pct_approx": weightage_pct,
            "canonical_topic_count": len(canonical_topics),
            "topics_covered": sorted(matched),
            "topics_covered_count": len(matched),
            "coverage_pct": coverage_pct,
            "coverage_basis": coverage_basis,
            "real_bank_total": real_total,
            "mastery_pct": round(mastery_pct, 1),
            "avg_stability": round(avg_stability, 2),
            "avg_difficulty": round(avg_difficulty, 2),
            "questions_solved": questions_solved,
            "unmatched_topic_folders": sorted(unmatched),
            "priority_score": priority_score,
            "estimated_yield_marks": estimated_yield_marks,
            "yield_effort_ratio": yield_effort_ratio
        }

    # Cross-subject penalty for Cascading Weaknesses
    # Simple algorithm: if Renal/Medicine mastery is low, penalize Anatomy/Pharma
    if subject_insights.get("Medicine", {}).get("mastery_pct", 100) < 40:
        # Penalize Pharmacology and Anatomy
        for s in ["Pharmacology", "Anatomy"]:
            if s in subject_insights:
                subject_insights[s]["priority_score"] += 1.5
                subject_insights[s]["estimated_yield_marks"] += 0.5
                
    if subject_insights.get("Pathology", {}).get("mastery_pct", 100) < 40:
        if "Medicine" in subject_insights:
            subject_insights["Medicine"]["priority_score"] += 2.0

    total_weight = sum(s["weightage_pct_approx"] for s in subject_insights.values())
    weighted_mastery = (
        sum(s["mastery_pct"] * s["weightage_pct_approx"] for s in subject_insights.values()) / total_weight
        if total_weight else 0.0
    )

    ranked_priority = sorted(subject_insights.items(), key=lambda kv: kv[1]["priority_score"], reverse=True)
    # Highest yield-to-effort
    ranked_yield = sorted(subject_insights.items(), key=lambda kv: kv[1]["yield_effort_ratio"], reverse=True)
    
    touched = [(k, v) for k, v in subject_insights.items() if v["questions_solved"] > 0]
    ranked_strength = sorted(touched, key=lambda kv: kv[1]["mastery_pct"], reverse=True)
    untouched = sorted(
        [(k, v) for k, v in subject_insights.items() if v["questions_solved"] == 0],
        key=lambda kv: kv[1]["weightage_pct_approx"], reverse=True
    )
    data_quality_flags = [
        {"subject": k, "unmatched_topic_folders": v["unmatched_topic_folders"]}
        for k, v in subject_insights.items() if v["unmatched_topic_folders"]
    ]

    return {
        "subjects": subject_insights,
        "overall_weighted_coverage_pct": round(weighted_mastery, 1), # Backward compatibility name
        "overall_weighted_mastery_pct": round(weighted_mastery, 1),
        "focus_next": [k for k, _ in ranked_priority[:5]],
        "highest_yield_targets": [k for k, _ in ranked_yield[:3]],
        "strongest_subjects": [k for k, _ in ranked_strength[:3]],
        "untouched_high_yield_subjects": [k for k, _ in untouched],
        "data_quality_flags": data_quality_flags
    }

def update_daily_metrics():
    """Updates study_metrics.json with detailed analytics."""
    today = datetime.date.today().isoformat()
    total_q, subject_counts, topic_counts, topic_mastery = scan_qbank_detailed()

    metrics = {
        "target_daily_questions": 300,
        "exam_date": "2026-08-30",
        "subject_matrix": ALL_19_SUBJECTS,
        "daily_logs": {}
    }

    if os.path.exists(METRICS_FILE):
        try:
            with open(METRICS_FILE, 'r') as f:
                existing = json.load(f)
                metrics.update(existing)
        except Exception:
            pass

    if "daily_logs" not in metrics:
        metrics["daily_logs"] = {}

    log = metrics["daily_logs"].get(today, {
        "date": today,
        "questions_completed": 0,
        "cumulative_total": 0,
        "subject_breakdown": {},
        "topic_breakdown": {},
        "topic_mastery": {},
        "sessions": [],
        "active_hours": 0.0,
        "speed_q_per_hour": 0.0,
        "endurance_score": 0
    })

    # total_q is a lifetime cumulative scan of QBank/, not "added today" --
    # derive today's actual count as the delta over the most recent prior
    # day's cumulative snapshot, so it resets at midnight instead of just
    # re-stamping the running lifetime total onto whatever date is "today".
    prior_dates = sorted(d for d in metrics["daily_logs"] if d < today)
    if prior_dates:
        prior_log = metrics["daily_logs"][prior_dates[-1]]
        prior_cumulative = prior_log.get("cumulative_total", prior_log.get("questions_completed", 0))
    else:
        prior_cumulative = 0
    today_completed = max(0, total_q - prior_cumulative)

    log["questions_completed"] = today_completed
    log["cumulative_total"] = total_q
    log["subject_breakdown"] = subject_counts
    log["topic_breakdown"] = topic_counts
    log["topic_mastery"] = topic_mastery

    # Score concentration for pomodoro sessions logged via /session (tab switches + time away).
    focus_scores = []
    for s in log["sessions"]:
        if "tab_switches" in s or "blur_seconds" in s:
            score = max(0, 100 - s.get("tab_switches", 0) * 5 - (s.get("blur_seconds", 0) // 10))
            s["concentration_score"] = int(score)
            if s.get("session_type", "focus") == "focus":
                focus_scores.append(score)
    log["concentration_score"] = round(sum(focus_scores) / len(focus_scores), 1) if focus_scores else None

    focus_sessions = [s for s in log["sessions"] if s.get("session_type", "focus") == "focus"]
    if focus_sessions:
        total_seconds = 0
        for s in focus_sessions:
            try:
                st = datetime.datetime.fromisoformat(s["start_time"])
                et = datetime.datetime.fromisoformat(s["end_time"])
                total_seconds += (et - st).total_seconds()
            except Exception:
                pass
        hours = total_seconds / 3600.0
        log["active_hours"] = round(hours, 2)
        log["speed_q_per_hour"] = round(today_completed / hours, 1) if hours > 0 else 0
    else:
        est_hours = round(today_completed / 38.9, 2) if today_completed > 0 else 0
        log["active_hours"] = est_hours
        log["speed_q_per_hour"] = round(today_completed / est_hours, 1) if est_hours > 0 else 0

    target = metrics.get("target_daily_questions", 300)
    completion_ratio = min(today_completed / target, 1.0)
    endurance_score = int(completion_ratio * 85 + min(log["speed_q_per_hour"] / 40.0, 1.0) * 15)
    log["endurance_score"] = min(endurance_score, 100)

    metrics["daily_logs"][today] = log
    metrics["syllabus_insights"] = compute_syllabus_insights(subject_counts, topic_counts, load_reference(), load_bank_totals())
    metrics["question_type_distribution"] = scan_qbank_question_types()
    metrics["dropzone_performance"] = scan_dropzone_companions()

    with open(METRICS_FILE, 'w') as f:
        json.dump(metrics, f, indent=2)

    return metrics

if __name__ == '__main__':
    data = update_daily_metrics()
    print("ATHENA Detailed Analytics Updated:")
    print(json.dumps(data, indent=2))
