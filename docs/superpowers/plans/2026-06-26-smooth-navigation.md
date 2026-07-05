# Smooth Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make left-sidebar navigation in the Node1 user service feel smooth, while improving 3X-UI panel loading through reverse-proxy caching/compression.

**Architecture:** Keep the current single-file Python app and server-rendered pages. Add a partial-navigation response mode used by a small client script to swap only the main content, update the breadcrumb/title/sidebar active state, and fall back to normal navigation on failure. Keep 3X-UI source untouched and optimize only Caddy headers/compression.

**Tech Stack:** Python `http.server`, SQLite, vanilla JavaScript, CSS, Caddy.

---

### Task 1: Partial Page Response

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`
- Test: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`

- [ ] Add a failing unit test that calls a new helper and expects a JSON payload with `title`, `crumb`, `content`, and `path`.
- [ ] Implement `build_partial_payload(title, content, path)`.
- [ ] Update `render()` to return JSON when request header `X-Node1-Partial: 1` is present.
- [ ] Run `python3 test_affiliate_accounting.py`.

### Task 2: Smooth Client Navigation

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`
- Modify: `/Volumes/littlejiang02/科学上网/style.css.affiliates`
- Test: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`

- [ ] Add a failing test that rendered HTML contains the `data-node1-nav` app marker and the partial navigation header string.
- [ ] Add `data-node1-nav` to the layout, `data-current-path` to the body, and `data-nav-link` to internal menu links.
- [ ] Add vanilla JS click interception for internal GET links, `fetch()` with `X-Node1-Partial: 1`, content swap, history update, active menu update, loading state, and fallback.
- [ ] Add CSS for smooth fade/loading states.
- [ ] Run `python3 test_affiliate_accounting.py`.

### Task 3: Static Asset Cache

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`
- Test: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`

- [ ] Add a failing test for `static_cache_headers()`.
- [ ] Implement `static_cache_headers()` and use it in `static_css()`.
- [ ] Run `python3 test_affiliate_accounting.py`.

### Task 4: Deploy And Verify

**Files:**
- Deploy local `/Volumes/littlejiang02/科学上网/app.py.affiliates` to `/opt/subscription-shop/app.py`.
- Deploy local `/Volumes/littlejiang02/科学上网/style.css.affiliates` to `/opt/subscription-shop/static/style.css`.
- Adjust `/opt/talking202605/cloud-deploy/caddy/Caddyfile` for 3X-UI proxy compression/cache only if validation shows the route can be safely isolated.

- [ ] Back up server app and CSS.
- [ ] Compile Python on the server.
- [ ] Restart `subscription-shop.service`.
- [ ] Verify `https://node1.talking202606.dpdns.org/login` and admin pages.
- [ ] Verify partial response with `X-Node1-Partial: 1`.
- [ ] Verify 3X-UI panel remains reachable.
