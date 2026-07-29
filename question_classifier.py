#!/usr/bin/env python3
"""
METIS/ATHENA — NEET PG question-type classifier.

Classifies each QBank MCQ stem into one of the question archetypes that
actually recur in this QBank (derived by inspecting the real 104 questions,
not guessed): Clinical Vignette, Except/Negative, Multi-Statement Evaluation,
and Simple/Direct Recall. Also flags image-based ("spotter") questions as a
cross-cutting tag since that's a distinct skill regardless of stem type.

Heuristic, not ML: regex over the question stem text. Good enough to route
"which type of question is costing me marks" analytics; not a clinical NLP
system.
"""
import re

VIGNETTE_RE = re.compile(
    r'^A[n]?\s+(\d+[-\s]?(year|month|day)s?[-\s]?old|\w+[-\s]old)\b'
    r'|^A[n]?\s+(man|woman|boy|girl|child|infant|neonate|patient|elderly|cyclist|athlete|worker)\b'
    r'|^(A \d+|An? \d+)'
    r'|\b(presents? with|comes? in with|is (examined|found to have|brought|admitted)|'
    r'develops (sudden|acute)|is thrown|lands on|thrown over)\b',
    re.IGNORECASE
)

EXCEPT_RE = re.compile(
    r'\bEXCEPT\b\s*[:.]?\s*$|\bEXCEPT\b.{0,15}$'
    r'|is NOT (a|an|the)\b'
    r'|are NOT\b'
    r'|which of the following is NOT\b',
    re.IGNORECASE
)

MULTI_STATEMENT_RE = re.compile(
    r'which of the following statements?.{0,40}(are|is)\s+(correct|true)\b'
    r'|decide which of the (statements|pairings).{0,20}(below|are)\s*(correct)?'
    r'|identify the correct statements',
    re.IGNORECASE
)

IMAGE_RE = re.compile(
    r'\(image shows|rhythm strip is (shown|recorded)|ecg (is )?shown|is shown\)'
    r'|x-ray (is )?shown|shown\.|illustrated,|the image (shown|below)',
    re.IGNORECASE
)


def classify_question(stem):
    """Returns (primary_type, is_image_based)."""
    is_image = bool(IMAGE_RE.search(stem))

    if EXCEPT_RE.search(stem):
        return 'Except_Negative', is_image
    if MULTI_STATEMENT_RE.search(stem):
        return 'Multi_Statement_Evaluation', is_image
    if VIGNETTE_RE.search(stem.strip()):
        return 'Clinical_Vignette', is_image
    return 'Simple_Direct_Recall', is_image


TYPE_LABELS = {
    'Clinical_Vignette': 'Clinical Vignette',
    'Except_Negative': 'Except / Negative ("all EXCEPT", "which is NOT")',
    'Multi_Statement_Evaluation': 'Multi-Statement Evaluation ("which are true/correct")',
    'Simple_Direct_Recall': 'Simple / Direct Recall',
}

if __name__ == '__main__':
    import os
    import glob

    QBANK_DIR = '/home/ubuntu/vp/NEET_PG/QBank'
    counts = {}
    image_count = 0
    total = 0
    samples = {}

    for path in glob.glob(os.path.join(QBANK_DIR, '**', '*.md'), recursive=True):
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        for m in re.finditer(r'\*\*Question:\*\*\s*(.+)', content):
            stem = m.group(1).strip()
            qtype, is_image = classify_question(stem)
            counts[qtype] = counts.get(qtype, 0) + 1
            if is_image:
                image_count += 1
            total += 1
            samples.setdefault(qtype, []).append(stem[:100])

    print(f"Classified {total} questions:\n")
    for qtype, label in TYPE_LABELS.items():
        n = counts.get(qtype, 0)
        pct = round(n / total * 100, 1) if total else 0
        print(f"  {label}: {n} ({pct}%)")
        for s in samples.get(qtype, [])[:2]:
            print(f"      e.g. \"{s}...\"")
    print(f"\n  Image-based (spotter) flag set on: {image_count} ({round(image_count/total*100,1)}%)")
