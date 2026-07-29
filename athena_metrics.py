#!/usr/bin/env python3
import os
import re
import json
import datetime
import glob

from question_classifier import classify_question, TYPE_LABELS

QBANK_DIR = '/home/ubuntu/vp/NEET_PG/QBank'
DROPZONE_DIR = '/home/ubuntu/vp/NEET_PG/Dropzone'
METRICS_FILE = '/home/ubuntu/vp/NEET_PG/study_metrics.json'
REFERENCE_FILE = '/home/ubuntu/vp/NEET_PG/subject_topics_reference.json'

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
                    
                    # Target floor 50 Qs per subtopic for 100% initial mastery score
                    mastery = min(round((count / 50.0) * 100, 1), 100.0)
                    topic_mastery[topic_key] = {
                        "count": count,
                        "mastery_percent": mastery,
                        "status": "High Mastery" if mastery >= 75 else ("Solid" if mastery >= 40 else "Needs Focus")
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

def compute_syllabus_insights(subject_counts, topic_counts, reference):
    """Coverage % and exam-weighted priority per subject, using the researched
    subject_topics_reference.json taxonomy as the ground truth checklist."""
    subject_insights = {}

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
        coverage_pct = round(len(matched) / len(canonical_topics) * 100, 1) if canonical_topics else 0.0

        subject_insights[subject] = {
            "weightage_pct_approx": weightage_pct,
            "canonical_topic_count": len(canonical_topics),
            "topics_covered": sorted(matched),
            "topics_covered_count": len(matched),
            "coverage_pct": coverage_pct,
            "questions_solved": subject_counts.get(subject, 0),
            "unmatched_topic_folders": sorted(unmatched),
            # Higher = bigger gap in a heavier-weighted subject = solve here next.
            "priority_score": round((1 - coverage_pct / 100.0) * weightage_pct, 2)
        }

    total_weight = sum(s["weightage_pct_approx"] for s in subject_insights.values())
    weighted_coverage = (
        sum(s["coverage_pct"] * s["weightage_pct_approx"] for s in subject_insights.values()) / total_weight
        if total_weight else 0.0
    )

    ranked_priority = sorted(subject_insights.items(), key=lambda kv: kv[1]["priority_score"], reverse=True)
    touched = [(k, v) for k, v in subject_insights.items() if v["questions_solved"] > 0]
    ranked_strength = sorted(touched, key=lambda kv: kv[1]["coverage_pct"], reverse=True)
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
        "overall_weighted_coverage_pct": round(weighted_coverage, 1),
        "focus_next": [k for k, _ in ranked_priority[:5]],
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
        "subject_breakdown": {},
        "topic_breakdown": {},
        "topic_mastery": {},
        "sessions": [],
        "active_hours": 0.0,
        "speed_q_per_hour": 0.0,
        "endurance_score": 0
    })

    log["questions_completed"] = total_q
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
        hours = max(total_seconds / 3600.0, 0.5)
        log["active_hours"] = round(hours, 2)
        log["speed_q_per_hour"] = round(total_q / hours, 1)
    else:
        est_hours = round(total_q / 38.9, 2) if total_q > 0 else 0
        log["active_hours"] = est_hours
        log["speed_q_per_hour"] = round(total_q / est_hours, 1) if est_hours > 0 else 0

    target = metrics.get("target_daily_questions", 300)
    completion_ratio = min(total_q / target, 1.0)
    endurance_score = int(completion_ratio * 85 + min(log["speed_q_per_hour"] / 40.0, 1.0) * 15)
    log["endurance_score"] = min(endurance_score, 100)

    metrics["daily_logs"][today] = log
    metrics["syllabus_insights"] = compute_syllabus_insights(subject_counts, topic_counts, load_reference())
    metrics["question_type_distribution"] = scan_qbank_question_types()
    metrics["dropzone_performance"] = scan_dropzone_companions()

    with open(METRICS_FILE, 'w') as f:
        json.dump(metrics, f, indent=2)

    return metrics

if __name__ == '__main__':
    data = update_daily_metrics()
    print("ATHENA Detailed Analytics Updated:")
    print(json.dumps(data, indent=2))
