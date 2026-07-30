#!/usr/bin/env python3
"""
ATHENA Insights Agent — runs the deterministic metrics refresh, then asks
agy for a short qualitative read on the topic-weighted subject matrix
(what's falling behind relative to exam weightage, what to hit next).
Reports the result to NYX via nyx_notify (TL4 safe-zone, METIS/ATHENA scope
per .agents/standard_protocol.md) rather than talking to VPK directly.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/ubuntu/vp/.scripts")
from nyx_notify import notify, notify_error

from athena_metrics import update_daily_metrics, METRICS_FILE

AGY_BIN = "/home/ubuntu/.local/bin/agy"
IST = timezone(timedelta(hours=5, minutes=30))


def build_prompt(insights):
    subjects = insights["subjects"]
    focus_next = insights["focus_next"]
    highest_yield = insights.get("highest_yield_targets", [])
    untouched = insights["untouched_high_yield_subjects"]

    lines = [
        "You are ATHENA, a predictive AI reviewing a NEET-PG aspirant's QBank and Anki (FSRS) progress.",
        f"Overall weighted syllabus mastery (coverage + retention): {insights.get('overall_weighted_mastery_pct', insights.get('overall_weighted_coverage_pct'))}%.",
        "",
        "Top-priority subjects (dynamic penalty based on decay and poor retention):",
    ]
    for subj in focus_next:
        s = subjects[subj]
        lines.append(
            f"- {subj}: Mastery {s.get('mastery_pct')}% (Coverage {s['coverage_pct']}%, FSRS Stability {s.get('avg_stability', 0)}), "
            f"Exam Weight ~{s['weightage_pct_approx']}%, Priority Score {s['priority_score']}"
        )
        
    if highest_yield:
        lines.append("")
        lines.append("Highest Yield-to-Effort Opportunities (max marks for least effort):")
        for subj in highest_yield:
            s = subjects[subj]
            lines.append(
                f"- {subj}: Yield {s.get('estimated_yield_marks', 0)} marks. Avg Difficulty: {s.get('avg_difficulty', 0)}/10. "
                f"(Ratio: {s.get('yield_effort_ratio', 0)})"
            )

    if untouched:
        lines.append("")
        lines.append("Untouched high-yield subjects: " + ", ".join(untouched[:5]))

    lines.append("")
    lines.append(
        "In under 120 words, give a highly dynamic, algorithmic read: "
        "Point out specific retention decay risks (e.g. 'You're forgetting Surgery'), and which 1-2 subjects to attack next based on the highest yield vs effort ratio. "
        "No preamble, no restating the raw numbers. Talk like an AI data scientist."
    )
    return "\n".join(lines)


def run_agy(prompt):
    result = subprocess.run(
        [AGY_BIN, "--dangerously-skip-permissions", "--print-timeout", "3m", "--print", prompt],
        capture_output=True,
        text=True,
        timeout=200,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agy exited {result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


def main():
    metrics = update_daily_metrics()
    insights = metrics.get("syllabus_insights")
    if not insights:
        notify_error("ATHENA", "Insights agent found no syllabus_insights in study_metrics.json — skipping.")
        return

    try:
        commentary = run_agy(build_prompt(insights))
    except Exception as e:
        notify_error("ATHENA", "3-hourly insights generation failed.", detail=str(e))
        return

    insights["ai_commentary"] = commentary
    insights["ai_commentary_generated_at"] = datetime.now(IST).isoformat()

    with open(METRICS_FILE, "r") as f:
        metrics = json.load(f)
    metrics["syllabus_insights"] = insights
    with open(METRICS_FILE, "w") as f:
        json.dump(metrics, f, indent=2)

    notify("ATHENA", f"Syllabus insights refreshed ({insights['overall_weighted_coverage_pct']}% weighted coverage). {commentary[:180]}")


if __name__ == "__main__":
    main()
