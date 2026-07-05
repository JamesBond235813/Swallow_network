# iOS Shadowrocket Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Node1 user service clearly support iOS Shadowrocket-style clients across product cards, dashboard copy actions, subscription cards, docs, and verified link output.

**Architecture:** Reuse the existing `/sub/<sub_id>` VLESS single-node endpoint as the Shadowrocket-compatible import source. Keep `/clashx/<sub_id>` for Clash/Mihomo YAML subscriptions. Update user-facing text and buttons so users choose the correct import path without adding duplicate backend routes.

**Tech Stack:** Python `http.server`, SQLite, vanilla HTML/CSS, existing unittest suite.

---

### Task 1: Tests For iOS Compatibility

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/test_affiliate_accounting.py`
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`

- [ ] Add a failing test that `build_node_url()` returns a `vless://` URL with REALITY fields suitable for Shadowrocket-style manual import.
- [ ] Add a failing test that plan cards and docs mention `iOS` and `Shadowrocket`.
- [ ] Add a failing test that `shortcut_panel()` and `subscription_card()` expose a `复制 Shadowrocket 节点` action.
- [ ] Run `python3 test_affiliate_accounting.py` and verify the new tests fail before implementation.

### Task 2: User-Facing Support

**Files:**
- Modify: `/Volumes/littlejiang02/科学上网/app.py.affiliates`

- [ ] Update plan benefits from `支持 Windows / Android Clash 类应用` to include iOS.
- [ ] Update docs to explain Windows/Android Clash/Mihomo, iOS Shadowrocket, and iOS Stash/Quantumult X fallback behavior.
- [ ] Update dashboard shortcut labels and descriptions so Shadowrocket users copy the single-node VLESS link.
- [ ] Update subscription card copy labels.
- [ ] Run `python3 test_affiliate_accounting.py`.

### Task 3: Deploy And Verify

**Files:**
- Deploy local `/Volumes/littlejiang02/科学上网/app.py.affiliates` to `/opt/subscription-shop/app.py`.

- [ ] Back up server app file.
- [ ] Compile Python on the server.
- [ ] Restart `subscription-shop.service`.
- [ ] Verify `/plans` includes iOS/Shadowrocket copy.
- [ ] Verify `/docs` includes the iOS instructions.
- [ ] Verify `/sub/<known_id>` returns a `vless://` link.
