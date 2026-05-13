# Roadmap — StrongPass Competition OS
**Last updated:** 2026-05-13

---

## Legend
- ✅ Done
- 🔄 In progress
- ⏳ Planned
- 💡 Idea / under consideration

---

## Completed

### Infrastructure & Deployment
- ✅ Deploy to Hetzner (Ubuntu 24.04, Gunicorn, Nginx, SSL)
- ✅ Systemd service with auto-restart
- ✅ Let's Encrypt SSL
- ✅ Gunicorn initialization fix (Phase 1)
- ✅ SSE migration — 98.8% polling reduction (Phase 2)
- ✅ Partial modularisation — app/ package (Phase 3)
- ✅ Performance optimization — results cache + change detection (Phase 4 perf)
- ✅ Beta access gate — sitewide cookie auth + robots.txt noindex

---

## Active Priorities (Product Roadmap)

### P2 — Security Audit
Audit all endpoints for injection, auth gaps, and CSRF exposure.  
**Scope:** Unauthenticated write endpoints, CSRF on all forms, input validation, CORS tightening.  
**Blockers:** None  
**Output:** Updated `known_issues.md`, prioritized fix list

### P3 — Mobile-First Redesign Planning
Current judge.html and comp pages are desktop-optimized. Competition judges use phones/tablets.  
**Scope:** Define breakpoints, layout changes, touch targets for judge.html, comp_run.html  
**Output:** Design doc + wireframes in docs/

### P4 — Organiser/Member Architecture Planning
Define multi-tenant model: organisers own competitions, members register for events.  
**Key decisions:**
- Single-tenant now → multi-tenant later, or design multi-tenant from scratch?
- User model: who is an "organiser"? OAuth or credential-based?
- Data isolation: per-organiser DB vs shared DB with org_id FK?
- How does existing competition engine map to this model?  
**Output:** Architecture decision doc in docs/

### P5 — Stripe Subscription Planning
Billing model for organiser tier access.  
**Key decisions:**
- Per-event pricing vs monthly subscription vs freemium?
- What's included in free tier?
- Stripe Billing or Stripe Checkout?
- Webhook handling for subscription state  
**Output:** Billing design doc in docs/

### P6 — Organiser Access Control
Implement role-based access: organiser role can manage their competitions.  
**Depends on:** P4 architecture decision  
**Scope:** Auth model, session management, permission checks, organiser dashboard

### P7 — Members Area Architecture
Athlete profiles, competition history, registration flow.  
**Depends on:** P4 (user model), P6 (auth)  
**Scope:** Athlete accounts, personal results pages, event registration

### P8 — Livestream / Content Platform Roadmap
Video integration, VOD hosting, content delivery for competitions.  
**Key decisions:** Self-hosted vs third-party (Mux, Cloudflare Stream)?  
**Output:** Platform decision doc in docs/

---

## Technical Debt Roadmap

### Next technical work (before P3+)

| Phase | Item | Priority | Depends on |
|-------|------|----------|-----------|
| Scoring | Weight & Reps event type | High | Nothing |
| Security | CSRF protection on all forms | High | Nothing |
| DB | Request-scoped connection (Flask `g`) | Medium | Nothing |
| DB | Fix bare `except: pass` in migrations | Medium | Nothing |
| Leaderboard | Per-event breakdown in comp_leaderboard.html | Medium | Nothing |
| Hardening | `/health` endpoint | Medium | Nothing |
| Hardening | Structured logging (replace bare except) | Medium | Nothing |
| Hardening | `requirements.txt` with pinned versions | Low | Nothing |
| Frontend | Shared CSS base template | Low | Nothing |
| Frontend | Delete `ws-client.js` (all overlays use SSE now) | Low | Verify no remaining usage |
| Auth | Document judge endpoint auth model for prod use | Low | Security audit |

### Longer-term technical

| Item | Notes |
|------|-------|
| Full modularisation (app/routes/, app/services/) | Phase 3 extracted helpers; routes still in monolith |
| Separate `broadcast_state.json` from competition config | state.json is dual-purpose (TD-M2) |
| CATEGORY_ORDER in DB instead of global mutable list | TD-C2 — safe with 1 worker only |
| Docker / docker-compose | For reproducible deploys |
| PostgreSQL migration path | SQLite adequate for single-event; needed for multi-tenant |

---

## Deployment History

| Date | Change |
|------|--------|
| 2026-05-11 | Initial Hetzner deploy (nginx, gunicorn, certbot, systemd) |
| 2026-05-11 | Phase 1–4 deployed (Gunicorn fix, SSE, modularisation, perf) |
| 2026-05-13 | Beta auth gate deployed; BETA_TOKEN + COMP_PASSWORD set |
