# Service Operations Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing subscription shop into a usable service operations backend while preserving the current mobile browser experience.

**Architecture:** Keep the current single-file Python service for this first tranche, because production already runs it and the test suite targets it directly. Add small database-backed operations primitives: node registry, fair-use policy settings, high-consumption detection, and admin search views. Do not change public mobile layout or subscription import formats in this tranche.

**Tech Stack:** Python stdlib HTTP server, SQLite, 3X-UI SQLite integration, existing `unittest` suite.

---

### Task 1: Protect Mobile Surface And Add Operations Tests

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`

- [ ] Add tests that assert the admin sidebar exposes new PC operations entries while the user mobile bottom tabs and public plan cards keep their existing labels and copy actions.
- [ ] Add tests for node registry seeding and admin node table rendering.
- [ ] Add tests for fair-use policy defaults and high-consumption subscription detection.
- [ ] Add tests that `/admin/subscriptions?q=...&risk=high` filters rows by user email, subscription label, and fair-use status.
- [ ] Run the targeted tests and verify they fail because the new operations functions and routes do not exist yet.

### Task 2: Add Service Operations Data Model

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`

- [ ] Extend `init_db()` with `nodes`, `fair_use_policies`, and `admin_audit_logs` tables.
- [ ] Seed the current US REALITY node from existing environment defaults.
- [ ] Seed default fair-use thresholds for unlimited plans: watch at 800GB/month, review at 1200GB/month, restrict at 1800GB/month.
- [ ] Add helpers for node status labels, fair-use threshold lookup, and subscription risk classification.
- [ ] Run the new data-model tests and verify they pass.

### Task 3: Add PC Admin Operations Views

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`

- [ ] Add `/admin/nodes` route with node table, capacity fields, status, and role/pool metadata.
- [ ] Add `/admin/fair-use` route with policy table and high-consumption subscription list.
- [ ] Add admin sidebar entries for `节点管理` and `公平使用`.
- [ ] Enhance `/admin` overview with operations stats: active users, active subscriptions, visible products, active nodes, high-risk users, total monthly usage.
- [ ] Run admin view tests and verify they pass.

### Task 4: Add Admin Search And Filters

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`

- [ ] Update `/admin/subscriptions` to read query string filters.
- [ ] Support `q` matching user email, subscription label, sub id, UUID, and product name.
- [ ] Support `risk=high` for review/restrict fair-use subscriptions.
- [ ] Render a compact search form above the subscriptions table.
- [ ] Run targeted subscription admin tests and verify they pass.

### Task 5: Full Verification And Production Diff Check

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`
- Modify: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`

- [ ] Run `python3 -m unittest test_affiliate_accounting.py`.
- [ ] Run a syntax check for `app.py.affiliates`.
- [ ] Review changed files and confirm no mobile CSS or public import format changed.
- [ ] Summarize what was implemented and what remains for the next tranche: multi-node assignment, low-speed pool, and deploy sync to `/opt/subscription-shop`.
