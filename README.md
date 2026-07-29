# 🦉 ATHENA & METIS — Medical Study & Anki Flashcard PWA System

> **Pantheon Education System** governing NEET PG Exam Preparation, QBank Analytics, and Spaced Repetition Flashcards.

---

## 🏛️ System Architecture

- **ATHENA (Goddess of Wisdom)**: Primary education & exam mastery agent. Tracks 300 Qs/day pacing across all 19 NEET PG medical subjects, calculates subject/subtopic mastery, and serves `athena_dashboard.html`.
- **METIS (Titaness of Wisdom & Learning)**: Flashcard Governor sub-agent reporting to ATHENA. Parses Markdown vault decks (`Flashcards/`), calculates dynamic SM-2 & FSRS 5.0 spaced repetition intervals, and serves `metis_dashboard.html`.

---

## 🎴 METIS Anki Flashcards PWA Features

1. **Dynamic Spaced Repetition Engine (SM-2 / FSRS)**:
   - Dynamic per-card intervals calculated in real-time (`1m`, `10m`, `1d`, `12d`, `25d`, `1.2mo`).
   - Card state progression (`new` &rarr; `learning` &rarr; `review` &rarr; `relearning`).
2. **Interactive Study Player**:
   - Keyboard Navigation: `SPACE` to reveal answer, `1` (Again), `2` (Hard), `3` (Good), `4` (Easy).
   - Audio feedback tones & progress bar.
   - High-yield takeaway callout rendering & Cloze deletion (`{{c1::...}}`).
3. **⚙️ Anki Options Configurator**:
   - Customize New Cards / Day, Max Reviews / Day, Learning Steps, Graduating Interval, and Easy Interval.
   - Toggle between Anki SM-2 Standard and FSRS 5.0 algorithms.
4. **PWA Standalone & Offline Support**:
   - Web App Manifest (`manifest.json`) and Service Worker (`sw.js`) for desktop and mobile home screen installation.

---

## 📁 Repository Structure

```
pantheon-athena-metis/
├── athena_dashboard.html   # ATHENA NEET PG Analytics & Control Center
├── athena_metrics.py       # QBank & Subject Mastery Scanner
├── metis_dashboard.html    # METIS Anki Flashcards Spaced Repetition PWA
├── metis_flashcards.py     # METIS Markdown Parser & SM-2/FSRS Engine
├── manifest.json           # Web App Manifest for PWA installation
├── sw.js                   # Service Worker for offline asset caching
├── study_metrics.json      # Single Source of Truth metrics file
├── Flashcards/             # Subject Markdown Decks (Anatomy, Medicine, Surgery, etc.)
└── QBank/                  # Subject Question Banks
```

---

## 🚀 Execution Commands

### Scan QBank Metrics
```bash
python3 athena_metrics.py
```

### Run METIS Flashcards Governor
```bash
python3 metis_flashcards.py
```

---
*Governed by: ZEUS ⚡, ATHENA 🦉 & METIS 🦉*
