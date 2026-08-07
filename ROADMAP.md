---
title: "pantheon-athena-metis Roadmap"
version: "1.0"
last_updated: 2026-08-03
status: Active
scope: >
  Product/feature roadmap for the three UI surfaces this pm2 process hosts:
  the Homepage PWA, the Athena Control Center, and the Metis flashcards PWA.
  MASTER_ROADMAP.md at the vault root covers the ecosystem-wide agent/infra
  view (including the METIS Python agent's role in the Discord/Telegram
  slash-command layer); this file is scoped to the standalone web UIs.
---

# pantheon-athena-metis Roadmap

Single pm2 process (`dropzone_server.py`) serving three related but distinct UIs, all static-file-fallback HTML/JS served from `/home/ubuntu/vp/NEET_PG/` (homepage) and `/home/ubuntu/pantheon-athena-metis/` (Athena/Metis):

1. **Homepage PWA** — phone home-screen dashboard (bento grid): clock, quick-add, On Duty tile, exam countdown, weather, merged Today (Obsidian + Vikunja), Finance/Wellness snapshots, Services.
2. **Athena Control Center** (`athena_dashboard.html`) — study/exam prep hub, links out to Metis.
3. **Metis flashcards PWA** (`metis_dashboard.html`) — Anki-style spaced-repetition flashcard review, real FSRS/SM-2 scheduling (`metis_db.py`), backed by `metis_sm2.py`/`metis_migrate.py`.

## Current State

- **Metis flashcard engine**: real per-user FSRS/SM-2 algorithm settings, daily caps, and a weight optimizer built directly into `metis_db.py` — not a toy scheduler.
- **Card auditor**: nightly flag-only duplicate/fact-check agent (`metis_card_auditor.py`), agent-based rather than embeddings (embeddings won't scale to the ~40k card target). Flags for review; doesn't auto-delete.
- **Handheld device**: a spare OnePlus Nord CE3 Lite repurposed as a dedicated Metis review device — Tailscale HTTPS (port 9443), kiosk mode, wake lock, swipe-to-rate. ESP32 hardware companion (ATHENA/METIS "Focus Scythe" concept in MASTER_ROADMAP.md's Phase 4 hardware list) is deferred, not built.
- **Homepage PWA**: built on the same stack as pantheon-tasks was originally built from (`dropzone_server.py`/`metis_db.py`), reusing `VikunjaHelper` for quick-add and merged Today list rather than duplicating a REST client. Finance/Wellness tiles read config/db files directly rather than importing the scheduled-agent scripts (`plutus_finance.py`, `hygieia_care.py`, etc.) to avoid triggering their Discord side effects on every page load.
- **Auth**: none. All three surfaces are reachable by anyone with the URL/Tailscale access; no login layer exists today.

## Recently Completed

- **2026-07-31** — Homepage PWA built: bento-grid layout, Vikunja quick-add (shells out to the shared `vk` CLI), merged Today list (Obsidian + Vikunja), Finance/Wellness snapshot tiles, themed scrollbars (standing design rule for all Pantheon PWAs).
- **v3.25 (2026-07-29, per MASTER_ROADMAP.md)** — METIS formally registered as Flashcard Governor sub-agent under ATHENA; Anki PWA (`metis_dashboard.html`) built and linked from the Athena Control Center.
- Metis handheld device pipeline (Tailscale HTTPS kiosk mode) completed.
- Metis card auditor (nightly, agent-based) completed.

## Known Issues / Tech Debt

- **No authentication on any of the three surfaces.** Lowest-hanging fruit for the passkey unification effort, since this is a single pm2 process — one login gate could plausibly cover all three UIs at once rather than needing three separate integrations.
- **`argus_location.py` redundancy** (surfaced 2026-07-31, not yet acted on) — a second, independent geofencing cron pulling Google Location Sharing via `locationsharinglib`, currently broken (stale cookies). Overlaps with the now-proven Tasker push pipeline; both can fire duplicate Discord notifications if both are alive. Recommendation on the table: retire `argus_location.py` rather than fix its cookies. Undecided.
- **Athena/Metis dashboards split across two codebases** — the HTML files live here, but `athena_dashboard.html`/`metis_dashboard.html` are also *served* as routes from `pantheon-server` (see that app's ROADMAP.md). Worth deciding which codebase should actually own them long-term.
- **ESP32 "Focus Scythe" hardware companion deferred** — no firmware or hardware work started; purely a MASTER_ROADMAP.md Phase 4 wishlist item (~₹2k BOM).

## Planned / Backlog

- [ ] **Passkey login** for Homepage/Athena/Metis, likely as one shared gate at the `dropzone_server.py` level rather than three separate integrations. See [[PASSKEY_UNIFICATION_ROADMAP]].
- [ ] Decide fate of `argus_location.py` (retire vs. fix cookies).
- [ ] Decide long-term home for `athena_dashboard.html`/`metis_dashboard.html` (here vs. pantheon-server).
- [ ] ESP32 "Focus Scythe" physical review-trigger device (deferred, no active work).
- [ ] **`metis-mcp` server** — flashcard review tools (get due queue, rate card, study stats), homepage snapshot, on-demand card audit trigger. See [[MCP_ECOSYSTEM_ROADMAP]] — third server proposed in the build sequence.
