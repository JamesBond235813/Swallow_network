import importlib.util
import io
import json
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.parse
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


APP_PATH = os.environ.get("APP_UNDER_TEST", "app.py.affiliates")


def load_app(db_path, xui_db_path=None, public_payment_base=None):
    os.environ["SHOP_DB"] = db_path
    os.environ["SHOP_SEED_DEMO_USER"] = "0"
    os.environ["XUI_MODE"] = "mock"
    if public_payment_base:
        os.environ["PUBLIC_PAYMENT_BASE"] = public_payment_base
    else:
        os.environ.pop("PUBLIC_PAYMENT_BASE", None)
    if xui_db_path:
        os.environ["XUI_DB_PATH"] = xui_db_path
        os.environ["XUI_INBOUND_ID"] = "2"
    os.environ["SHOP_ADMIN_EMAIL"] = "admin-test@example.com"
    os.environ["SHOP_ADMIN_PASSWORD"] = "admin-test-password"
    spec = importlib.util.spec_from_loader(
        "shop_app_under_test", SourceFileLoader("shop_app_under_test", APP_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.init_db()
    return module


class AffiliateAccountingTest(unittest.TestCase):
    def make_rsa_key_pair(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        return private_pem, public_pem

    def test_default_admin_password_is_not_reset_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_env = {
                key: os.environ.get(key)
                for key in (
                    "SHOP_DB",
                    "SHOP_SEED_DEMO_USER",
                    "XUI_MODE",
                    "SHOP_ADMIN_EMAIL",
                    "SHOP_ADMIN_PASSWORD",
                    "SHOP_SYNC_ADMIN_PASSWORD",
                )
            }
            try:
                os.environ["SHOP_DB"] = os.path.join(tmp, "shop.db")
                os.environ["SHOP_SEED_DEMO_USER"] = "0"
                os.environ["XUI_MODE"] = "mock"
                os.environ["SHOP_ADMIN_EMAIL"] = "admin@example.com"
                os.environ["SHOP_ADMIN_PASSWORD"] = "admin123456"
                os.environ.pop("SHOP_SYNC_ADMIN_PASSWORD", None)
                spec = importlib.util.spec_from_loader(
                    "shop_app_default_admin_test",
                    SourceFileLoader("shop_app_default_admin_test", APP_PATH),
                )
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)
                app.init_db()
                con = app.get_db()
                custom_hash = app.hash_password("changed-password")
                con.execute(
                    "update users set password_hash=? where email=?",
                    (custom_hash, "admin@example.com"),
                )
                con.commit()
                con.close()

                app.init_db()

                con = app.get_db()
                stored = con.execute(
                    "select password_hash from users where email=?",
                    ("admin@example.com",),
                ).fetchone()["password_hash"]
                con.close()
                self.assertTrue(app.verify_password("changed-password", stored))
                self.assertFalse(app.verify_password("admin123456", stored))
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_user_sidebar_is_minimal_but_legacy_routes_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.render("用户中心", "<section>Dashboard</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('href="/dashboard"', handler.captured_body)
            self.assertIn('href="/plans"', handler.captured_body)
            self.assertIn('href="/docs"', handler.captured_body)
            self.assertIn('href="/profile"', handler.captured_body)
            self.assertNotIn("我的工单", handler.captured_body)
            self.assertNotIn("流量明细", handler.captured_body)
            self.assertNotIn("节点状态", handler.captured_body)
            self.assertNotIn("side-group", handler.captured_body)

            handler.path = "/nodes"
            handler.get_nodes()
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("Node1 Direct", handler.captured_body)

    def test_dashboard_consolidates_account_node_and_support_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.get_dashboard()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("捷径", handler.captured_body)
            self.assertIn("一键配置", handler.captured_body)
            self.assertIn("邀请有奖励", handler.captured_body)
            self.assertNotIn("Node1 Direct", handler.captured_body)
            self.assertNotIn("工单未启用", handler.captured_body)
            self.assertNotIn("公告", handler.captured_body)
            self.assertNotIn("请勿公开转发订阅地址；发现泄漏后请联系管理员处理。", handler.captured_body)

    def test_docs_are_concise_for_existing_power_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/docs"
            handler.get_docs()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("Clash / Mihomo", handler.captured_body)
            self.assertIn("Shadowrocket", handler.captured_body)
            self.assertIn("Stash / Quantumult X", handler.captured_body)
            self.assertNotIn("面向普通用户和站点运营方", handler.captured_body)
            self.assertNotIn("隐私边界", handler.captured_body)

    def test_docs_page_uses_simple_config_help_with_collapsed_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/docs"
            handler.get_docs()

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("配置帮助", body)
            self.assertIn("安卓 / 电脑", body)
            self.assertIn("iOS / iPhone", body)
            self.assertIn("连不上时先看这三项", body)
            self.assertIn("仍然有问题", body)
            self.assertIn('class="support-feedback-collapsible"', body)
            self.assertNotIn("<h2>入口</h2>", body)
            self.assertIn('class="support-feedback-form"', body)
            self.assertIn('name="message"', body)
            self.assertIn("反馈问题", body)
            self.assertIn("提交反馈", body)
            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("docs-support-polish-20260627", css)
            self.assertIn("docs-simple-20260630", css)

    def test_user_feedback_can_be_submitted_and_admin_reply_is_shown_on_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return urllib.parse.urlencode(self.form).encode()

                def redirect(self, path):
                    self.redirect_path = path

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            submit = object.__new__(DummyHandler)
            submit.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            submit.form = {"message": "安卓端订阅导入失败，请帮我看一下。"}
            submit.post_support_feedback()
            self.assertEqual(submit.redirect_path, "/docs#feedback")

            con = app.get_db()
            feedback = con.execute("select * from support_feedback where user_id=?", (user_id,)).fetchone()
            con.close()
            self.assertIsNotNone(feedback)
            self.assertEqual(feedback["message"], "安卓端订阅导入失败，请帮我看一下。")
            self.assertEqual(feedback["status"], "open")

            admin_view = object.__new__(DummyHandler)
            admin_view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            admin_view.path = "/admin/feedbacks"
            admin_view.get_admin_feedbacks()
            self.assertEqual(admin_view.captured_status, 200)
            self.assertIn("用户反馈", admin_view.captured_body)
            self.assertIn("安卓端订阅导入失败", admin_view.captured_body)
            self.assertIn('name="reply"', admin_view.captured_body)

            reply = object.__new__(DummyHandler)
            reply.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            reply.form = {"feedback_id": str(feedback["id"]), "reply": "已收到，请先更换单节点导入。"}
            reply.post_admin_feedbacks_reply()
            self.assertEqual(reply.redirect_path, "/admin/feedbacks")

            user_view = object.__new__(DummyHandler)
            user_view.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            user_view.path = "/docs"
            user_view.get_docs()
            self.assertIn("已收到，请先更换单节点导入。", user_view.captured_body)
            self.assertIn("已回复", user_view.captured_body)

    def test_public_mobile_header_does_not_duplicate_login_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('href="/login"', handler.captured_body)
            self.assertIn('href="/register"', handler.captured_body)
            self.assertNotIn("未登录", handler.captured_body)
            self.assertNotIn("account-pill", handler.captured_body)

    def test_mobile_plan_facts_are_kept_in_one_row(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn(".plan-feature-grid", css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
        self.assertIn(".plan-feature-grid div", css)
        self.assertIn("padding: 7px 6px;", css)

    def test_mobile_layout_uses_bottom_tabs_and_top_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()

            css = Path("style.css.affiliates").read_text(encoding="utf-8")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("mobile-brand", handler.captured_body)
            self.assertIn("bottom: 0;", css)
            self.assertIn("padding-bottom: calc(76px + env(safe-area-inset-bottom));", css)
            self.assertIn(".side-brand {\n    display: none;", css)
            self.assertIn(".account-pill {\n    display: none;", css)

    def test_brand_uses_swallow_logo_and_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()
            body = handler.captured_body
            css = Path("style.css.affiliates").read_text(encoding="utf-8")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("style.css?v=docs-simple-20260630", body)
            self.assertIn("<title>套餐 · 燕子</title>", body)
            self.assertIn('src="/static/swallow-logo.png?v=swallow-brand-20260626"', body)
            self.assertIn('<meta name="theme-color" content="#ffffff">', body)
            self.assertIn('<link rel="manifest" href="/manifest.webmanifest?v=swallow-pwa-20260701-white-logo">', body)
            self.assertIn('<link rel="icon" type="image/png" href="/static/swallow-logo.png?v=swallow-brand-20260626">', body)
            self.assertIn('<link rel="icon" type="image/png" sizes="192x192" href="/static/android-chrome-192x192.png?v=swallow-pwa-20260701-white-logo">', body)
            self.assertIn('<link rel="icon" type="image/png" sizes="512x512" href="/static/android-chrome-512x512.png?v=swallow-pwa-20260701-white-logo">', body)
            self.assertIn('<link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=swallow-pwa-20260701-white-logo">', body)
            self.assertIn('<link rel="shortcut icon" href="/favicon.ico?v=swallow-brand-20260626">', body)
            self.assertIn('alt="燕子"', body)
            self.assertIn("brand-text", body)
            self.assertIn(">燕子<", body)
            self.assertNotIn(">小猫<", body)
            self.assertNotIn(">caty<", body)
            self.assertNotIn('<span class="brand-mark">N</span><span>Node1</span>', body)
            self.assertIn(".brand-logo", css)
            self.assertIn(".brand-text", css)
            self.assertIn("display: inline-flex;", css)
            self.assertIn("white-space: nowrap;", css)
            self.assertNotIn("text-transform: uppercase;", css)
            self.assertTrue(Path("static/swallow-logo.png").exists())
            self.assertTrue(Path("static/android-chrome-192x192.png").exists())
            self.assertTrue(Path("static/android-chrome-512x512.png").exists())
            self.assertTrue(Path("static/maskable-icon-512x512.png").exists())
            self.assertTrue(Path("static/apple-touch-icon.png").exists())

    def test_static_swallow_logo_is_served_as_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.path = "/static/swallow-logo.png"
            handler.do_GET()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(("Content-Type", "image/png"), handler.headers_out)
            self.assertTrue(handler.wfile.getvalue().startswith(b"\x89PNG"))

    def test_favicon_ico_uses_swallow_brand_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.path = "/favicon.ico"
            handler.do_GET()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(("Content-Type", "image/x-icon"), handler.headers_out)
            self.assertTrue(handler.wfile.getvalue().startswith(b"\x00\x00\x01\x00"))

    def test_webmanifest_exposes_android_home_screen_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.path = "/manifest.webmanifest"
            handler.do_GET()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(("Content-Type", "application/manifest+json; charset=utf-8"), handler.headers_out)
            manifest = json.loads(handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(manifest["short_name"], "燕子")
            self.assertEqual(manifest["display"], "standalone")
            self.assertIn(
                {
                    "src": "/static/android-chrome-192x192.png?v=swallow-pwa-20260701-white-logo",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                manifest["icons"],
            )
            self.assertIn(
                {
                    "src": "/static/maskable-icon-512x512.png?v=swallow-pwa-20260701-white-logo",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                manifest["icons"],
            )

    def test_android_home_screen_icon_is_served_as_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.path = "/static/android-chrome-192x192.png"
            handler.do_GET()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(("Content-Type", "image/png"), handler.headers_out)
            self.assertTrue(handler.wfile.getvalue().startswith(b"\x89PNG"))

    def test_wechat_verification_file_is_served_from_site_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.path = "/5dc498f857ec00ccebf236b3186b6d53.txt"
            handler.do_GET()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(("Content-Type", "text/plain; charset=utf-8"), handler.headers_out)
            self.assertEqual(handler.wfile.getvalue(), b"0a2e6a3c93ca0d249330b3622d9765355a4971f8")

            head_handler = object.__new__(DummyHandler)
            head_handler.headers = {}
            head_handler.headers_out = []
            head_handler.wfile = io.BytesIO()
            head_handler.path = "/5dc498f857ec00ccebf236b3186b6d53.txt"
            head_handler.do_HEAD()

            self.assertEqual(head_handler.captured_status, 200)
            self.assertEqual(head_handler.wfile.getvalue(), b"")

    def test_frontend_follows_accept_language_for_supported_languages(self):
        cases = [
            ("zh-Hant-TW,zh;q=0.9", 'lang="zh-TW"', "選擇最適合您的方案", "立即訂閱"),
            ("en-US,en;q=0.9", 'lang="en"', "Choose the plan that fits you", "Subscribe now"),
            ("fa-IR,fa;q=0.9", 'lang="fa" dir="rtl"', "طرح مناسب خود را انتخاب کنید", "اکنون مشترک شوید"),
            ("ja-JP,ja;q=0.9", 'lang="ja"', "最適なプランを選択", "今すぐ購読"),
            ("vi-VN,vi;q=0.9", 'lang="vi"', "Chọn gói phù hợp nhất", "Đăng ký ngay"),
            ("ko-KR,ko;q=0.9", 'lang="ko"', "가장 알맞은 요금제를 선택하세요", "지금 구독"),
        ]
        for accept_language, lang_attr, headline, cta in cases:
            with self.subTest(accept_language=accept_language):
                with tempfile.TemporaryDirectory() as tmp:
                    app = load_app(os.path.join(tmp, "shop.db"))

                    class DummyHandler(app.App):
                        def send_html(self, status, body, headers=None):
                            self.captured_status = status
                            self.captured_body = body

                    handler = object.__new__(DummyHandler)
                    handler.headers = {"Accept-Language": accept_language}
                    handler.path = "/plans"
                    handler.get_plans()

                    self.assertEqual(handler.captured_status, 200)
                    self.assertIn(lang_attr, handler.captured_body)
                    self.assertIn(headline, handler.captured_body)
                    self.assertIn(cta, handler.captured_body)

    def test_partial_payload_is_localized_for_accept_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_json(self, status, payload, headers=None):
                    self.captured_status = status
                    self.captured_payload = payload

            handler = object.__new__(DummyHandler)
            handler.headers = {"X-Node1-Partial": "1", "Accept-Language": "en-US,en;q=0.9"}
            handler.path = "/docs"
            handler.render("使用文档", "<section>套餐 账户 立即订阅</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertEqual(handler.captured_payload["title"], "Docs")
            self.assertEqual(handler.captured_payload["documentTitle"], "Docs · Swallow")
            self.assertIn("Plans", handler.captured_payload["content"])
            self.assertIn("Account", handler.captured_payload["content"])
            self.assertIn("Subscribe now", handler.captured_payload["content"])

    def test_admin_pages_remain_chinese_when_browser_language_is_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id)), "Accept-Language": "en-US,en;q=0.9"}
            handler.path = "/admin/products"
            handler.render("订阅套餐管理", "<section>订阅套餐</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('lang="zh-CN"', handler.captured_body)
            self.assertIn("订阅套餐管理", handler.captured_body)
            self.assertNotIn("Subscription Plan Management", handler.captured_body)

    def test_profile_contains_account_exit_action_for_mobile_account_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("buyer@example.com", handler.captured_body)
            self.assertIn('href="/logout"', handler.captured_body)

    def test_plan_cards_have_compact_mobile_action_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            css = Path("style.css.affiliates").read_text(encoding="utf-8")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("plan-card-main", handler.captured_body)
            self.assertIn("plan-actions", handler.captured_body)
            self.assertNotIn("分享套餐", handler.captured_body)
            self.assertIn(".plan-actions", css)
            self.assertIn("grid-template-columns: 1fr;", css)

    def test_dashboard_uses_mobile_function_distribution_on_desktop_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.get_dashboard()
            css = Path("style.css.affiliates").read_text(encoding="utf-8")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("mobile-dashboard-head", handler.captured_body)
            self.assertIn("mobile-hidden", handler.captured_body)
            self.assertIn("mobile-only", handler.captured_body)
            self.assertIn("n-card mobile-shortcuts", handler.captured_body)
            self.assertIn("一键配置", handler.captured_body)
            self.assertIn("邀请有奖励", handler.captured_body)
            self.assertIn("复制邀请链接", handler.captured_body)
            self.assertIn("复制订阅", handler.captured_body)
            self.assertNotIn("Node1 Direct", handler.captured_body)
            self.assertNotIn('id="orders"', handler.captured_body)
            self.assertIn("@media (max-width: 720px)", css)
            self.assertIn(".mobile-hidden", css)
            self.assertIn(".mobile-only", css)
            self.assertIn(".mobile-dashboard-head", css)
            self.assertIn(".sub-card .sub-actions", css)

    def test_profile_mobile_account_tab_contains_orders_and_reset_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            app.create_order_for_user(user_id, product_id, use_balance=True)
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("账户订单", handler.captured_body)
            self.assertIn("重置密码", handler.captured_body)
            self.assertIn("mobile-order-list", handler.captured_body)

    def test_mobile_ux_fix_uses_safe_area_touch_targets_and_bottom_sheets(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("mobile-ux-fix-20260626", css)
        self.assertIn("--mobile-tabbar-height: 76px;", css)
        self.assertIn("padding-bottom: calc(var(--mobile-tabbar-height) + 28px + env(safe-area-inset-bottom));", css)
        self.assertIn("min-height: 44px;", css)
        self.assertIn(".share-panel[open] .share-sheet", css)
        self.assertIn("position: fixed;", css)
        self.assertIn("bottom: calc(var(--mobile-tabbar-height) + env(safe-area-inset-bottom));", css)
        self.assertIn(".plan-highlights", css)
        self.assertIn("display: none;", css)

    def test_mobile_plan_cards_are_scan_first_without_share_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("plan-meta-line", handler.captured_body)
            self.assertIn("80 GB/月 / 月付 / 1 台设备共用", handler.captured_body)
            self.assertNotIn('data-share-mode="sheet"', handler.captured_body)
            self.assertNotIn("分享套餐", handler.captured_body)
            self.assertIn("plan-highlights", handler.captured_body)
            self.assertIn("低延迟 | 智能分流 | 高速稳定 | 甄选优质线路", handler.captured_body)
            self.assertIn("支持 iOS/ Android/ MacOS/ Windows全平台", handler.captured_body)

            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("plan-card-copy-polish-20260626", css)
            self.assertIn(".plan-highlights", css)
            self.assertIn("color: #5f6876;", css)

    def test_mobile_plan_cards_use_quiet_copy_hierarchy(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("mobile-plan-card-quiet-20260626", css)
        self.assertIn(".plan-card-main .plan-meta-line", css)
        self.assertIn("color: #6b7280;", css)
        self.assertIn(".plan-card-main .plan-highlights", css)
        self.assertIn("color: #7a8391;", css)
        self.assertIn("border-top: 1px solid #eef0f3;", css)
        self.assertIn(".plan-feature-grid {\n    display: none;", css)
        self.assertIn(".plan-actions {\n    padding: 0 14px 14px;", css)

    def test_mobile_plan_header_aligns_title_meta_left_and_badge_price_right(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("mobile-plan-header-align-20260626", css)
        self.assertIn(".plan-card-main .plan-head > div", css)
        self.assertIn("display: contents;", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", css)
        self.assertIn("grid-template-areas:", css)
        self.assertIn("\"title badge\"", css)
        self.assertIn("\"meta price\"", css)
        self.assertIn("grid-area: badge;", css)
        self.assertIn("justify-self: end;", css)
        self.assertIn(".plan-card-main .plan-price b", css)
        self.assertIn("font-size: 21px;", css)

    def test_created_checkout_order_is_not_a_pending_bill_until_payment_method_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            order = con.execute("select order_no,status,payment_method,payment_ref from orders where id=?", (order_id,)).fetchone()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            self.assertEqual(handler.captured_status, 200)
            self.assertEqual(order["status"], "created")
            self.assertEqual(order["payment_method"], "")
            self.assertIsNone(order["payment_ref"])
            self.assertNotIn(order["order_no"], handler.captured_body)
            self.assertNotIn("待支付", handler.captured_body)
            self.assertNotIn("mobile-order-action", handler.captured_body)
            self.assertNotIn("pending_payment", handler.captured_body)
            self.assertNotIn("provisioned", handler.captured_body)

    def test_mobile_account_orders_show_pending_bill_after_payment_method_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con.close()

            class PayHandler(app.App):
                def get_body(self):
                    return b"payment_method=manual"

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            pay_handler = object.__new__(PayHandler)
            pay_handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            pay_handler.path = f"/orders/{order_id}/pay"
            pay_handler.post_order_pay(f"/orders/{order_id}/pay")

            class ProfileHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(ProfileHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            con = app.get_db()
            order = con.execute("select order_no,status,payment_method,payment_ref from orders where id=?", (order_id,)).fetchone()
            con.close()

            self.assertEqual(pay_handler.captured_status, 200)
            self.assertEqual(order["status"], "pending_payment")
            self.assertEqual(order["payment_method"], "manual")
            self.assertTrue(order["payment_ref"].startswith("manual-review-"))
            self.assertEqual(handler.captured_status, 200)
            self.assertIn(order["order_no"], handler.captured_body)
            self.assertIn("待支付", handler.captured_body)
            self.assertIn("mobile-order-action", handler.captured_body)
            self.assertNotIn("pending_payment", handler.captured_body)

    def test_order_without_selected_payment_method_does_not_become_pending_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return b""

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = f"/orders/{order_id}/pay"
            handler.post_order_pay(f"/orders/{order_id}/pay")

            con = app.get_db()
            order = con.execute("select status,payment_method,payment_ref from orders where id=?", (order_id,)).fetchone()
            con.close()

            self.assertEqual(handler.captured_status, 400)
            self.assertEqual(order["status"], "created")
            self.assertEqual(order["payment_method"], "")
            self.assertIsNone(order["payment_ref"])

    def test_legacy_referrals_route_redirects_to_mobile_invite_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    pass

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.headers_out = []
            handler.path = "/referrals"
            handler.get_referrals()

            self.assertEqual(handler.captured_status, 303)
            self.assertIn(("Location", "/dashboard#invite"), handler.headers_out)

    def test_mobile_page_titles_are_hidden_without_removing_desktop_titles(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("mobile-detail-polish-20260626", css)
        self.assertIn(".page-title", css)
        self.assertIn(".mobile-dashboard-head", css)
        self.assertIn("display: none;", css)
        self.assertIn(".dashboard-head.mobile-hidden", css)

    def test_mobile_topbar_is_fixed_during_scroll(self):
        css = Path("style.css.affiliates").read_text(encoding="utf-8")

        self.assertIn("mobile-fixed-topbar-20260626", css)
        self.assertIn("top: 0;", css)
        self.assertIn("z-index: 90;", css)
        self.assertIn("padding-top: calc(var(--mobile-topbar-height) + 16px + env(safe-area-inset-top));", css)

    def test_mobile_subscription_card_uses_plan_name_usage_capacity_row_and_status_pill(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.execute(
                """
                create table client_traffics (
                    inbound_id integer,
                    email text,
                    up integer,
                    down integer
                )
                """
            )
            xui.execute(
                """
                create table inbound_client_ips (
                    id integer primary key autoincrement,
                    client_email text,
                    ips text
                )
                """
            )
            used_bytes = int(4.24733 * 1024**3)
            xui.execute(
                "insert into client_traffics(inbound_id,email,up,down) values(?,?,?,?)",
                (2, "user-1-uMobile", 0, used_bytes),
            )
            xui.execute(
                "insert into inbound_client_ips(client_email,ips) values(?,?)",
                ("user-1-uMobile", '[{"ip":"203.0.113.1"},{"ip":"203.0.113.2"},{"ip":"203.0.113.1"}]'),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OTESTMOBILE',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    order_id,
                    product_id,
                    "user-1-uMobile",
                    "uMobile",
                    "00000000-0000-4000-8000-000000000000",
                    80 * 1024**3,
                    1784905174143,
                    "https://node1.example/clashx/uMobile",
                    "https://node1.example/sub/uMobile",
                    "local",
                    "OK",
                    5,
                    app.now_ms(),
                ),
            )
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.get_dashboard()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("style.css?v=docs-simple-20260630", handler.captured_body)
            self.assertIn("mobile-sub-title", handler.captured_body)
            self.assertIn("轻量月付", handler.captured_body)
            self.assertIn("mobile-sub-stats", handler.captured_body)
            self.assertIn("mobile-sub-stat traffic", handler.captured_body)
            self.assertIn("4.25/80 GB", handler.captured_body)
            self.assertIn("mobile-sub-stat devices", handler.captured_body)
            self.assertIn("2/5", handler.captured_body)
            self.assertIn("mobile-sub-stat expiry", handler.captured_body)
            self.assertIn("2026-07-24", handler.captured_body)
            self.assertIn("22:59:34", handler.captured_body)
            self.assertNotIn("4.24733 GB", handler.captured_body)
            self.assertNotIn("<span>容量 80 GB</span>", handler.captured_body)
            self.assertNotIn("已用 4.25 GB / 总容量 80 GB", handler.captured_body)
            self.assertIn("mobile-sub-status", handler.captured_body)
            self.assertIn(">开通<", handler.captured_body)

    def test_dashboard_prefers_latest_active_subscription_over_old_used_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table client_traffics (
                    inbound_id integer,
                    email text,
                    up integer,
                    down integer,
                    last_online integer
                );
                create table inbound_client_ips (
                    id integer primary key autoincrement,
                    client_email text,
                    ips text
                );
                """
            )
            xui.execute(
                "insert into client_traffics(inbound_id,email,up,down,last_online) values(?,?,?,?,?)",
                (2, "user-1-old", 123, 456, 1782540249000),
            )
            xui.execute(
                "insert into inbound_client_ips(client_email,ips) values(?,?)",
                ("user-1-old", '[{"ip":"27.47.33.167"}]'),
            )
            xui.execute(
                "insert into client_traffics(inbound_id,email,up,down,last_online) values(?,?,?,?,?)",
                (2, "user-1-new", 0, 0, 0),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            user_id = app.create_customer_user("buyer@example.com", "password123")
            now = app.now_ms()
            con = app.get_db()
            old_product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('正在使用套餐','monthly',30,80,5,1900,1,1,?,?)
                """,
                (now, now),
            ).lastrowid
            new_product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('新开未用套餐','monthly',30,220,5,2900,1,2,?,?)
                """,
                (now, now),
            ).lastrowid
            old_order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OOLD',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, old_product_id, now, now),
            ).lastrowid
            new_order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('ONEW',?,?,2900,'provisioned','manual',?,?,2900)
                """,
                (user_id, new_product_id, now + 1, now + 1),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    old_order_id,
                    old_product_id,
                    "user-1-old",
                    "uOld",
                    "00000000-0000-4000-8000-000000000001",
                    80 * 1024**3,
                    now + 30 * 86400 * 1000,
                    "https://node1.example/clashx/uOld",
                    "https://node1.example/sub/uOld",
                    "local",
                    "OK",
                    5,
                    now,
                ),
            )
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    new_order_id,
                    new_product_id,
                    "user-1-new",
                    "uNew",
                    "00000000-0000-4000-8000-000000000002",
                    220 * 1024**3,
                    now + 60 * 86400 * 1000,
                    "https://node1.example/clashx/uNew",
                    "https://node1.example/sub/uNew",
                    "local",
                    "OK",
                    5,
                    now + 1,
                ),
            )
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.get_dashboard()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('<span class="mobile-sub-title">新开未用套餐</span>', handler.captured_body)
            self.assertIn("0/5", handler.captured_body)
            self.assertNotIn('<span class="mobile-sub-title">正在使用套餐</span>', handler.captured_body)

    def test_plan_tabs_have_real_filter_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('data-plan-filter="monthly"', handler.captured_body)
            self.assertIn('data-plan-filter="high"', handler.captured_body)
            self.assertIn('data-period="monthly"', handler.captured_body)
            self.assertIn('data-traffic-tier="high"', handler.captured_body)
            self.assertIn("card.hidden = !show", handler.captured_body)
            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("\n[hidden] {", css)
            self.assertIn(".plan[hidden]", css)
            self.assertIn("display: none !important;", css)
            self.assertIn("mobile-plan-tabs-even-20260626", css)
            self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
            self.assertIn("justify-content: center;", css)

    def test_mobile_profile_order_and_logout_position_are_account_orders_security_logout(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            app.create_order_for_user(user_id, product_id, use_balance=False)
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("mobile-profile-account", body)
            self.assertIn("mobile-profile-logout", body)
            self.assertNotIn("profile-inline-actions", body)
            self.assertLess(body.index("账户信息"), body.index("账户订单"))
            self.assertLess(body.index("账户订单"), body.index("账户安全"))
            self.assertLess(body.index("账户安全"), body.index("退出登录"))
            self.assertLess(body.index("重置密码"), body.index("退出登录"))

            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("mobile-account-security-polish-20260626", css)
            self.assertIn(".mobile-account-orders {\n    margin-bottom: 18px;", css)
            self.assertIn(".mobile-profile-logout {\n    display: inline-flex;", css)
            self.assertIn("align-items: center;", css)
            self.assertIn("justify-content: center;", css)
            self.assertIn("text-align: center;", css)
            self.assertIn("border: 1px solid #159957;", css)
            self.assertIn("color: #111827;", css)

    def test_mobile_pending_order_has_cancel_confirmation_and_cancel_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con.execute("update orders set status='pending_payment',payment_method='manual',payment_ref='manual-review-test' where id=?", (order_id,))
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

                def redirect(self, path):
                    self.redirect_path = path

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()
            body = handler.captured_body

            self.assertIn("mobile-order-actions", body)
            self.assertIn("mobile-order-cancel", body)
            self.assertIn("mobile-order-pay", body)
            self.assertLess(body.index("mobile-order-cancel"), body.index("mobile-order-pay"))
            self.assertIn("<dialog", body)
            self.assertIn("确认取消订单？", body)
            self.assertIn("不取消", body)
            self.assertIn("确定取消", body)
            self.assertLess(body.index("不取消"), body.index("确定取消"))
            self.assertIn(f'action="/orders/{order_id}/cancel"', body)

            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("mobile-order-actions-inline-20260626", css)
            self.assertIn(".mobile-order-actions .mobile-order-action,\n  .mobile-dialog-actions .mobile-order-action", css)
            self.assertIn("grid-column: auto;", css)
            self.assertIn("display: inline-flex;", css)
            self.assertIn("justify-content: center;", css)

            cancel_handler = object.__new__(DummyHandler)
            cancel_handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            cancel_handler.post_order_cancel(f"/orders/{order_id}/cancel")

            con = app.get_db()
            status = con.execute("select status from orders where id=?", (order_id,)).fetchone()["status"]
            con.close()
            self.assertEqual(cancel_handler.redirect_path, "/profile")
            self.assertEqual(status, "cancelled")

    def test_checkout_created_order_can_be_cancelled_before_it_becomes_pending_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            status = con.execute("select status from orders where id=?", (order_id,)).fetchone()["status"]
            con.close()

            class DummyHandler(app.App):
                def redirect(self, path):
                    self.redirect_path = path

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.post_order_cancel(f"/orders/{order_id}/cancel")

            con = app.get_db()
            new_status = con.execute("select status from orders where id=?", (order_id,)).fetchone()["status"]
            con.close()

            self.assertEqual(status, "created")
            self.assertEqual(handler.redirect_path, "/profile")
            self.assertEqual(new_status, "cancelled")

    def test_mobile_account_orders_hide_cancelled_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            cancelled_order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            visible_order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con.execute("update orders set status='cancelled' where id=?", (cancelled_order_id,))
            con.execute("update orders set status='pending_payment',payment_method='manual',payment_ref='manual-review-test' where id=?", (visible_order_id,))
            cancelled_no = con.execute("select order_no from orders where id=?", (cancelled_order_id,)).fetchone()["order_no"]
            visible_no = con.execute("select order_no from orders where id=?", (visible_order_id,)).fetchone()["order_no"]
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn(visible_no, handler.captured_body)
            self.assertNotIn(cancelled_no, handler.captured_body)
            self.assertNotIn("已取消", handler.captured_body)

    def test_paid_user_profile_identity_uses_five_diamond_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("paid@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con = app.get_db()
            con.execute("update orders set status='paid', paid_at=?, updated_at=? where id=?", (app.now_ms(), app.now_ms(), order_id))
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/profile"
            handler.get_profile()

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("diamond-rank", body)
            self.assertIn('<svg class="diamond-icon"', body)
            self.assertIn('viewBox="0 0 1024 1024"', body)
            self.assertIn('width="18" height="18"', body)
            self.assertIn('fill="#1296db"', body)
            self.assertIn("M512.8 216l185.6 200H327.2l185.6-200z", body)
            self.assertEqual(body.count('class="diamond-icon"'), 5)
            self.assertNotIn("<dt>身份</dt><dd>普通用户</dd>", body)

    def test_admin_sidebar_labels_products_as_subscription_plan_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/products"
            handler.render("订阅套餐管理", "<section>Plans</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("订阅套餐", handler.captured_body)
            self.assertIn('href="/admin/products"', handler.captured_body)
            self.assertIn('data-nav-key="admin-products"', handler.captured_body)

    def test_service_ops_tables_seed_current_node_and_fair_use_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()

            node = con.execute("select * from nodes where code='node1-us'").fetchone()
            policy = con.execute("select * from fair_use_policies where product_family='unlimited'").fetchone()
            con.close()

            self.assertIsNotNone(node)
            self.assertEqual(node["region"], "US")
            self.assertEqual(node["inbound_id"], app.XUI_INBOUND_ID)
            self.assertEqual(node["public_host"], app.NODE_PUBLIC_HOST)
            self.assertEqual(node["status"], "active")
            self.assertIsNotNone(policy)
            self.assertEqual(policy["watch_gb"], 800)
            self.assertEqual(policy["review_gb"], 1200)
            self.assertEqual(policy["restrict_gb"], 1800)

    def test_admin_sidebar_adds_pc_ops_entries_without_mobile_user_nav_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            admin = object.__new__(DummyHandler)
            admin.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            admin.path = "/admin"
            admin.render("管理后台", "<section>Admin</section>")

            self.assertIn('href="/admin/nodes"', admin.captured_body)
            self.assertIn("节点管理", admin.captured_body)
            self.assertIn('href="/admin/fair-use"', admin.captured_body)
            self.assertIn("公平使用", admin.captured_body)

            public = object.__new__(DummyHandler)
            public.headers = {}
            public.path = "/plans"
            public.get_plans()

            self.assertIn("支持 iOS/ Android/ MacOS/ Windows全平台", public.captured_body)
            self.assertIn("立即订阅", public.captured_body)
            self.assertNotIn("节点管理", public.captured_body)
            self.assertNotIn("公平使用", public.captured_body)

    def test_admin_home_visibly_surfaces_service_ops_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin"
            handler.get_admin()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("运营工作台", handler.captured_body)
            self.assertIn("节点状态", handler.captured_body)
            self.assertIn("Node1 US Direct", handler.captured_body)
            self.assertIn("公平使用监控", handler.captured_body)
            self.assertIn("不限量", handler.captured_body)
            self.assertIn("定向查找", handler.captured_body)
            self.assertIn('class="admin-body"', handler.captured_body)
            self.assertIn('class="shell admin-shell"', handler.captured_body)
            self.assertNotIn('<div class="actions"><a class="button" href="/admin/subscriptions">订阅管理</a>', handler.captured_body)

    def test_inactive_product_is_hidden_from_purchase_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            con.execute("delete from products")
            con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,sort_order,created_at,updated_at)
                values('Visible Plan','monthly',30,80,1900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            )
            con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,sort_order,created_at,updated_at)
                values('Hidden Plan','monthly',30,80,1900,0,2,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            )
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("Visible Plan", handler.captured_body)
            self.assertNotIn("Hidden Plan", handler.captured_body)

    def test_plan_badges_recommend_unlimited_and_mark_others_as_enjoy(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            con.execute("delete from products")
            now = app.now_ms()
            con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量月付','subscription','unlimited','monthly',30,0,2900,1,1,1,?,?)
                """,
                (now, now),
            )
            con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('冲浪月付','subscription','limited','monthly',30,220,1900,1,1,2,?,?)
                """,
                (now, now),
            )
            con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('畅享流量包','traffic_pack','traffic_pack','traffic_pack',30,3000,74900,1,1,3,?,?)
                """,
                (now, now),
            )
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()

            self.assertIn("不限量月付", handler.captured_body)
            self.assertIn('<span class="plan-badge">推荐</span>', handler.captured_body)
            self.assertEqual(handler.captured_body.count('<span class="plan-badge">畅享</span>'), 2)
            self.assertNotIn('<span class="plan-badge">入门</span>', handler.captured_body)
            self.assertNotIn('<span class="plan-badge">流量包</span>', handler.captured_body)

    def test_admin_can_disable_and_enable_subscription_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,sort_order,created_at,updated_at)
                values('Toggle Plan','monthly',30,80,1900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def redirect(self, path):
                    self.redirect_path = path

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}

            handler.post_admin_product_status(f"/admin/products/{product_id}/disable")
            con = app.get_db()
            inactive = con.execute("select active from products where id=?", (product_id,)).fetchone()["active"]
            con.close()
            self.assertEqual(inactive, 0)
            self.assertEqual(handler.redirect_path, "/admin/products")

            handler.post_admin_product_status(f"/admin/products/{product_id}/enable")
            con = app.get_db()
            active = con.execute("select active from products where id=?", (product_id,)).fetchone()["active"]
            con.close()
            self.assertEqual(active, 1)

    def test_admin_can_delete_product_without_breaking_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.execute("delete from products")
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('Delete Plan','monthly',30,80,1900,1,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def redirect(self, path):
                    self.redirect_path = path

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            view = object.__new__(DummyHandler)
            view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            view.path = "/admin/products"
            view.get_admin_products()
            self.assertIn(f"/admin/products/{product_id}/delete", view.captured_body)
            self.assertIn("删除", view.captured_body)

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.post_admin_product_delete(f"/admin/products/{product_id}/delete")
            self.assertEqual(handler.redirect_path, "/admin/products")

            con = app.get_db()
            product = con.execute("select active,display_in_plans,deleted_at from products where id=?", (product_id,)).fetchone()
            con.close()
            self.assertEqual(product["active"], 0)
            self.assertEqual(product["display_in_plans"], 0)
            self.assertGreater(product["deleted_at"], 0)

            view = object.__new__(DummyHandler)
            view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            view.path = "/admin/products"
            view.get_admin_products()
            self.assertNotIn("Delete Plan", view.captured_body)

    def test_existing_order_keeps_product_snapshot_after_product_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            now = app.now_ms()
            con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,traffic_reset_days,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('旧套餐','subscription','limited','monthly',30,80,30,2,1900,1,1,10,?,?)
                """,
                (now, now),
            )
            product_id = con.execute("select last_insert_rowid()").fetchone()[0]
            con.commit()
            con.close()

            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con = app.get_db()
            con.execute(
                """
                update products
                set name='新套餐', duration_days=365, traffic_gb=500, traffic_reset_days=30, device_limit=9, price_cents=9900, updated_at=?
                where id=?
                """,
                (app.now_ms(), product_id),
            )
            con.execute("update orders set status='paid', paid_at=?, updated_at=? where id=?", (app.now_ms(), app.now_ms(), order_id))
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("旧套餐", handler.captured_body)
            self.assertIn("80 GB/月", handler.captured_body)
            self.assertIn("月付", handler.captured_body)
            self.assertIn("2 台设备", handler.captured_body)
            self.assertIn("¥19.00", handler.captured_body)
            self.assertNotIn("新套餐", handler.captured_body)
            self.assertNotIn("500 GB/月", handler.captured_body)
            self.assertNotIn("9 台设备", handler.captured_body)

            app.provision_order(order_id)
            con = app.get_db()
            sub = con.execute("select traffic_bytes,traffic_reset_days,device_limit from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()

            self.assertEqual(sub["traffic_bytes"], 80 * 1024**3)
            self.assertEqual(sub["traffic_reset_days"], 30)
            self.assertEqual(sub["device_limit"], 2)

    def test_billing_period_expiry_uses_calendar_cycle_not_duration_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            def local_ms(year, month, day, hour, minute, second):
                return int(app.time.mktime((year, month, day, hour, minute, second, 0, 0, -1)) * 1000)

            jan_start = local_ms(2026, 1, 1, 12, 0, 0)
            feb_start = local_ms(2026, 2, 2, 13, 42, 47)

            self.assertEqual(
                app.periodic_subscription_expires_at(jan_start, "monthly"),
                local_ms(2026, 1, 31, 11, 59, 59),
            )
            self.assertEqual(
                app.periodic_subscription_expires_at(feb_start, "monthly"),
                local_ms(2026, 3, 1, 13, 42, 46),
            )
            self.assertEqual(
                app.periodic_subscription_expires_at(feb_start, "quarterly"),
                local_ms(2026, 5, 1, 13, 42, 46),
            )
            self.assertEqual(
                app.periodic_subscription_expires_at(feb_start, "yearly"),
                local_ms(2027, 2, 1, 13, 42, 46),
            )

    def test_provisioned_subscription_uses_paid_at_for_billing_period_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            def local_ms(year, month, day, hour, minute, second):
                return int(app.time.mktime((year, month, day, hour, minute, second, 0, 0, -1)) * 1000)

            paid_at = local_ms(2026, 2, 2, 13, 42, 47)
            con = app.get_db()
            now = app.now_ms()
            con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,traffic_reset_days,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('自然月套餐','subscription','limited','monthly',30,80,30,2,1900,1,1,10,?,?)
                """,
                (now, now),
            )
            product_id = con.execute("select last_insert_rowid()").fetchone()[0]
            con.commit()
            con.close()

            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con = app.get_db()
            con.execute("update orders set status='paid', paid_at=?, updated_at=? where id=?", (paid_at, paid_at, order_id))
            con.commit()
            con.close()

            app.provision_order(order_id)

            con = app.get_db()
            sub = con.execute("select expires_at from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(sub["expires_at"], local_ms(2026, 3, 1, 13, 42, 46))

    def test_new_provisioned_subscription_replaces_existing_active_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_inbounds (
                    client_id integer,
                    inbound_id integer,
                    flow_override text,
                    created_at integer,
                    unique(client_id, inbound_id)
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            old_client = {
                "email": "user-1-uOld",
                "enable": True,
                "id": "00000000-0000-4000-8000-000000000001",
                "subId": "uOld",
            }
            xui.execute("insert into inbounds(id, settings) values(2, ?)", (json.dumps({"clients": [old_client]}),))
            xui.execute(
                "insert into clients(email,sub_id,uuid,flow,limit_ip,total_gb,expiry_time,enable,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                ("user-1-uOld", "uOld", old_client["id"], "xtls-rprx-vision", 5, 80 * 1024**3, 1784905174143, 1, 1, 1),
            )
            xui.execute(
                "insert into client_traffics(inbound_id,enable,email,up,down,expiry_time,total,reset,last_online) values(?,?,?,?,?,?,?,?,?)",
                (2, 1, "user-1-uOld", 0, 0, 1784905174143, 80 * 1024**3, 30, 1),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            old_order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,paid_at,created_at,updated_at,gross_amount_cents)
                values('OOLD',?,?,1900,'provisioned','manual',?,?,?,1900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms(), app.now_ms()),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,traffic_reset_days,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    old_order_id,
                    product_id,
                    "user-1-uOld",
                    "uOld",
                    old_client["id"],
                    80 * 1024**3,
                    30,
                    app.now_ms() + 30 * 86400 * 1000,
                    "https://node1.example/clashx/uOld",
                    "https://node1.example/sub/uOld",
                    "local",
                    "OK",
                    5,
                    app.now_ms(),
                ),
            )
            con.commit()
            con.close()

            new_order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con = app.get_db()
            con.execute(
                "update orders set status='paid', paid_at=?, updated_at=? where id=?",
                (app.now_ms(), app.now_ms(), new_order_id),
            )
            con.commit()
            con.close()

            app.provision_order(new_order_id)

            con = app.get_db()
            old_sub = con.execute("select * from subscriptions where sub_id='uOld'").fetchone()
            new_sub = con.execute("select * from subscriptions where order_id=?", (new_order_id,)).fetchone()
            con.close()
            self.assertIsNotNone(new_sub)
            self.assertGreater(old_sub["revoked_at"], 0)
            self.assertEqual(old_sub["revoked_by_order_id"], new_order_id)
            self.assertEqual(old_sub["revoked_reason"], "replaced")
            self.assertEqual(old_sub["xui_status"], "replaced")
            self.assertEqual(app.subscription_unavailable_reason(old_sub), "subscription replaced")
            self.assertIsNone(app.subscription_unavailable_reason(new_sub))

            xui = sqlite3.connect(xui_db)
            xui.row_factory = sqlite3.Row
            old_client_row = xui.execute("select enable from clients where email='user-1-uOld'").fetchone()
            old_traffic_row = xui.execute("select enable from client_traffics where email='user-1-uOld'").fetchone()
            inbound = xui.execute("select settings from inbounds where id=2").fetchone()
            xui.close()
            settings = json.loads(inbound["settings"])
            old_settings_client = [c for c in settings["clients"] if c["email"] == "user-1-uOld"][0]
            self.assertEqual(old_client_row["enable"], 0)
            self.assertEqual(old_traffic_row["enable"], 0)
            self.assertFalse(old_settings_client["enable"])

            class TextHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    pass

            handler = object.__new__(TextHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.get_clash_subscription("/clashx/uOld")
            self.assertEqual(handler.captured_status, 410)
            self.assertIn(b"subscription replaced", handler.wfile.getvalue())

    def test_admin_product_form_and_table_include_device_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/products"
            handler.get_admin_products()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn('name="device_limit"', handler.captured_body)
            self.assertIn("设备数", handler.captured_body)
            self.assertIn("<th>设备</th>", handler.captured_body)
            self.assertIn("<th>名称</th><th>排序</th><th>类别</th><th>形态</th>", handler.captured_body)
            row = app.product_row({
                "id": 999,
                "name": "Sort Plan",
                "product_type": "subscription",
                "duration_days": 30,
                "traffic_gb": 100,
                "device_limit": 1,
                "price_cents": 1900,
                "active": 1,
                "sort_order": 42,
            })
            self.assertIn("<td>Sort Plan</td><td>42</td><td>限量</td><td>周期套餐</td>", row)
            self.assertIn("<td>100 GB/月</td>", row)

    def test_admin_products_support_family_and_plan_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return urllib.parse.urlencode(self.form).encode()

                def redirect(self, path):
                    self.redirect_path = path

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            view = object.__new__(DummyHandler)
            view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            view.path = "/admin/products"
            view.get_admin_products()

            self.assertIn('name="product_family"', view.captured_body)
            self.assertIn('name="display_in_plans"', view.captured_body)
            self.assertIn("<th>类别</th>", view.captured_body)
            self.assertIn("<th>展示</th>", view.captured_body)

            row = app.product_row({
                "id": 1001,
                "name": "Hidden Unlimited",
                "product_type": "subscription",
                "product_family": "unlimited",
                "billing_period": "monthly",
                "duration_days": 30,
                "traffic_gb": 0,
                "device_limit": 5,
                "price_cents": 2900,
                "active": 1,
                "display_in_plans": 0,
                "sort_order": 10,
            })
            self.assertIn("<td>不限量</td>", row)
            self.assertIn("<td>隐藏</td>", row)

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.form = {
                "name": "不限量季付",
                "product_type": "subscription",
                "product_family": "unlimited",
                "billing_period": "quarterly",
                "duration_days": "90",
                "traffic_gb": "0",
                "device_limit": "5",
                "price_yuan": "68",
                "sort_order": "11",
                "active": "on",
            }
            handler.post_admin_products()

            con = app.get_db()
            product = con.execute("select product_family, display_in_plans from products where name=?", ("不限量季付",)).fetchone()
            con.close()
            self.assertEqual(product["product_family"], "unlimited")
            self.assertEqual(product["display_in_plans"], 0)

    def test_admin_product_create_edit_persists_device_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return urllib.parse.urlencode(self.form).encode()

                def redirect(self, path):
                    self.redirect_path = path

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.form = {
                "name": "Device Plan",
                "billing_period": "monthly",
                "duration_days": "30",
                "traffic_gb": "100",
                "device_limit": "3",
                "price_yuan": "29",
                "sort_order": "7",
                "active": "on",
            }
            handler.post_admin_products()

            con = app.get_db()
            product = con.execute("select * from products where name=?", ("Device Plan",)).fetchone()
            con.close()
            self.assertEqual(product["device_limit"], 3)

            handler.form = {
                "name": "Device Plan",
                "billing_period": "monthly",
                "duration_days": "30",
                "traffic_gb": "100",
                "device_limit": "5",
                "price_yuan": "29",
                "sort_order": "7",
                "active": "on",
            }
            handler.post_admin_product_edit(f"/admin/products/{product['id']}/edit")

            con = app.get_db()
            updated = con.execute("select device_limit from products where id=?", (product["id"],)).fetchone()
            con.close()
            self.assertEqual(updated["device_limit"], 5)

    def test_admin_product_create_edit_supports_traffic_pack_and_unlimited_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return urllib.parse.urlencode(self.form).encode()

                def redirect(self, path):
                    self.redirect_path = path

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            view = object.__new__(DummyHandler)
            view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            view.path = "/admin/products"
            view.get_admin_products()
            self.assertIn('name="product_type"', view.captured_body)
            self.assertIn("流量包", view.captured_body)
            self.assertIn('name="duration_days" type="number" min="0"', view.captured_body)
            self.assertIn('data-duration-field hidden', view.captured_body)
            self.assertIn("周期套餐按月付/季付/年付自动计算截止时间", view.captured_body)

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.form = {
                "name": "100G 流量包",
                "product_type": "traffic_pack",
                "billing_period": "traffic_pack",
                "duration_days": "0",
                "traffic_gb": "100",
                "device_limit": "2",
                "price_yuan": "25",
                "sort_order": "9",
                "active": "on",
            }
            handler.post_admin_products()

            con = app.get_db()
            product = con.execute("select * from products where name=?", ("100G 流量包",)).fetchone()
            con.close()
            self.assertEqual(product["product_type"], "traffic_pack")
            self.assertEqual(product["billing_period"], "traffic_pack")
            self.assertEqual(product["duration_days"], 0)
            self.assertEqual(app.product_type_label(product["product_type"]), "流量包")
            self.assertEqual(app.duration_label(product["duration_days"]), "不限期")

            edit_view = object.__new__(DummyHandler)
            edit_view.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            edit_view.path = f"/admin/products/{product['id']}/edit"
            edit_view.get_admin_product_edit(f"/admin/products/{product['id']}/edit")
            self.assertIn('data-duration-field>', edit_view.captured_body)
            self.assertNotIn('data-duration-field hidden', edit_view.captured_body)

            handler.form = {
                "name": "100G 流量包",
                "product_type": "traffic_pack",
                "billing_period": "traffic_pack",
                "duration_days": "60",
                "traffic_gb": "100",
                "device_limit": "2",
                "price_yuan": "25",
                "sort_order": "9",
                "active": "on",
            }
            handler.post_admin_product_edit(f"/admin/products/{product['id']}/edit")
            con = app.get_db()
            updated = con.execute("select product_type,duration_days from products where id=?", (product["id"],)).fetchone()
            con.close()
            self.assertEqual(updated["product_type"], "traffic_pack")
            self.assertEqual(updated["duration_days"], 60)

    def test_provisioned_subscription_snapshots_product_device_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('Device Limited','monthly',30,80,3,1900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('ODEVICE',?,?,1900,'paid','manual',?,?,1900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()

            app.provision_order(order_id)

            con = app.get_db()
            sub = con.execute("select device_limit from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(sub["device_limit"], 3)

    def test_plan_and_checkout_show_device_limit_before_purchase(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            con.execute("delete from products")
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('Three Device','monthly',30,120,3,2900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()
            self.assertIn("3 台设备", handler.captured_body)

            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()
            self.assertIn("3 台设备", handler.captured_body)

    def test_zero_traffic_plan_is_unlimited_in_public_display_and_provisioning(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            con.execute("delete from products")
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('Unlimited Plan','monthly',30,0,3,4900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()
            self.assertIn("不限GB / 月付 / 3 台设备共用", handler.captured_body)
            self.assertNotIn("0 GB / 月付", handler.captured_body)

            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()
            self.assertIn("不限GB / 月付 / 3 台设备", handler.captured_body)

            con = app.get_db()
            con.execute("update orders set status='paid' where id=?", (order_id,))
            con.commit()
            con.close()
            app.provision_order(order_id)
            con = app.get_db()
            sub = con.execute("select traffic_bytes from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(sub["traffic_bytes"], 0)

    def test_plans_hide_private_variants_but_checkout_switches_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            con.execute("delete from products")
            now = app.now_ms()
            monthly_id = con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量月付','subscription','unlimited','monthly',30,0,5,2900,1,1,10,?,?)
                """,
                (now, now),
            ).lastrowid
            quarterly_id = con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量季付','subscription','unlimited','quarterly',90,0,5,6800,1,0,11,?,?)
                """,
                (now, now),
            ).lastrowid
            yearly_id = con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量年付','subscription','unlimited','yearly',365,0,5,19900,1,0,12,?,?)
                """,
                (now, now),
            ).lastrowid
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return urllib.parse.urlencode(self.form).encode()

                def redirect(self, path):
                    self.redirect_path = path

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()
            self.assertIn("不限量月付", handler.captured_body)
            self.assertNotIn("不限量季付", handler.captured_body)
            self.assertNotIn("不限量年付", handler.captured_body)

            order_id = app.create_order_for_user(user_id, monthly_id, use_balance=False)
            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()
            body = handler.captured_body
            self.assertIn("checkout-period-tabs", body)
            self.assertIn("月付", body)
            self.assertIn("季付", body)
            self.assertIn("年付", body)
            self.assertIn(f'value="{quarterly_id}"', body)
            self.assertIn(f'value="{yearly_id}"', body)
            self.assertIn("¥29.00", body)

            switch_handler = object.__new__(DummyHandler)
            switch_handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            switch_handler.form = {"product_id": str(quarterly_id)}
            switch_handler.post_order_switch_product(f"/orders/{order_id}/switch-product")
            self.assertEqual(switch_handler.redirect_path, f"/checkout?id={order_id}")

            con = app.get_db()
            order = con.execute("select product_id,gross_amount_cents,amount_cents,balance_discount_cents from orders where id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(order["product_id"], quarterly_id)
            self.assertEqual(order["gross_amount_cents"], 6800)
            self.assertEqual(order["amount_cents"], 6800)
            self.assertEqual(order["balance_discount_cents"], 0)

            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()
            self.assertIn("不限量季付", handler.captured_body)
            self.assertIn("¥68.00", handler.captured_body)

    def test_subscription_products_treat_traffic_as_monthly_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            con.execute("delete from products")
            product_id = con.execute(
                """
                insert into products(name,product_type,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('标准季度','subscription','quarterly',90,720,10,9900,1,40,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/plans"
            handler.get_plans()
            self.assertIn("720 GB/月 / 季付 / 10 台设备共用", handler.captured_body)

            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()
            self.assertIn("720 GB/月 / 季付 / 10 台设备", handler.captured_body)

            con = app.get_db()
            con.execute("update orders set status='paid' where id=?", (order_id,))
            con.commit()
            con.close()
            app.provision_order(order_id)
            con = app.get_db()
            sub = con.execute("select traffic_bytes, traffic_reset_days from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(sub["traffic_bytes"], 720 * 1024**3)
            self.assertEqual(sub["traffic_reset_days"], 30)

    def test_traffic_pack_can_be_unlimited_period_and_ends_when_traffic_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.execute(
                """
                create table client_traffics (
                    inbound_id integer,
                    email text,
                    up integer,
                    down integer
                )
                """
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            con.execute("delete from products")
            product_id = con.execute(
                """
                insert into products(name,product_type,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('100G 流量包','traffic_pack','traffic_pack',0,100,1,2500,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class HtmlHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            plan_handler = object.__new__(HtmlHandler)
            plan_handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            plan_handler.path = "/plans"
            plan_handler.get_plans()
            self.assertIn("100 GB / 不限期 / 1 台设备共用", plan_handler.captured_body)
            self.assertNotIn("100 GB/月 / 不限期", plan_handler.captured_body)
            self.assertIn("流量包", plan_handler.captured_body)

            con = app.get_db()
            con.execute("update orders set status='paid' where id=?", (order_id,))
            con.commit()
            con.close()
            app.provision_order(order_id)

            con = app.get_db()
            sub = con.execute("select * from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(sub["expires_at"], 0)
            self.assertEqual(sub["traffic_reset_days"], 0)
            self.assertFalse(app.subscription_is_time_expired(sub))

            xui = sqlite3.connect(xui_db)
            xui.execute(
                "insert into client_traffics(inbound_id,email,up,down) values(?,?,?,?)",
                (2, sub["email_label"], 0, 100 * 1024**3),
            )
            xui.commit()
            xui.close()

            class TextHandler(app.App):
                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    pass

            handler = object.__new__(TextHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.get_clash_subscription(f"/clashx/{sub['sub_id']}")

            self.assertEqual(handler.captured_status, 410)
            self.assertIn(b"subscription traffic exhausted", handler.wfile.getvalue())

    def test_admin_product_form_allows_zero_traffic_for_unlimited_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/products"
            handler.get_admin_products()

            self.assertIn('name="traffic_gb" type="number" min="0"', handler.captured_body)

    def test_partial_payload_contains_navigation_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            payload = app.build_partial_payload("套餐", "<section>Plans</section>", "/plans")

            self.assertEqual(payload["title"], "套餐")
            self.assertEqual(payload["crumb"], "套餐")
            self.assertEqual(payload["path"], "/plans")
            self.assertIn("Plans", payload["content"])
            self.assertEqual(payload["bodyClass"], "")
            self.assertEqual(payload["layoutClass"], "app-layout")
            self.assertEqual(payload["shellClass"], "shell")

            admin_payload = app.build_partial_payload(
                "管理后台",
                "<section>Admin</section>",
                "/admin",
                is_admin_workspace=True,
            )
            self.assertEqual(admin_payload["bodyClass"], "admin-body")
            self.assertEqual(admin_payload["layoutClass"], "app-layout admin-layout")
            self.assertEqual(admin_payload["shellClass"], "shell admin-shell")

    def test_rendered_page_contains_smooth_navigation_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.render("套餐", "<section>Plans</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("data-node1-nav", handler.captured_body)
            self.assertIn("X-Node1-Partial", handler.captured_body)
            self.assertIn("data-nav-link", handler.captured_body)
            self.assertIn("applyLayoutClasses(payload)", handler.captured_body)
            self.assertIn("crossesWorkspace", handler.captured_body)

    def test_admin_default_home_is_admin_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin = con.execute("select * from users where email=?", ("admin-test@example.com",)).fetchone()
            customer_id = app.create_customer_user("buyer@example.com", "password123")
            customer = con.execute("select * from users where id=?", (customer_id,)).fetchone()
            con.close()

            self.assertEqual(app.default_home_for_user(admin), "/admin")
            self.assertEqual(app.default_home_for_user(customer), "/dashboard")

    def test_register_requires_email_but_existing_plain_admin_gets_login_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            con.execute("insert into users(email,password_hash,role,created_at) values(?,?,?,?)", ("xiaojiang", app.hash_password("Cn12345678"), "admin", app.now_ms()))
            con.commit()
            con.close()

            email_id = app.create_customer_user("buyer@example.com", "password123")

            con = app.get_db()
            email = con.execute("select email from users where id=?", (email_id,)).fetchone()["email"]
            con.close()

            self.assertEqual(email, "buyer@example.com")
            self.assertTrue(app.email_identifier_valid("buyer@example.com"))
            self.assertFalse(app.email_identifier_valid("xiaojiang"))
            self.assertTrue(app.user_identifier_exists("xiaojiang"))

            class DummyHandler(app.App):
                def get_body(self):
                    return b"email=xiaojiang&password=Cn12345678"

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/register"
            handler.post_register()

            self.assertEqual(handler.captured_status, 400)
            self.assertIn("账号已存在，请直接登录", handler.captured_body)
            self.assertNotIn("注册请使用邮箱", handler.captured_body)

    def test_partial_render_returns_json_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_json(self, status, payload, headers=None):
                    self.captured_status = status
                    self.captured_payload = payload

            handler = object.__new__(DummyHandler)
            handler.headers = {"X-Node1-Partial": "1"}
            handler.path = "/docs"
            handler.render("使用文档", "<section>Docs</section>")

            self.assertEqual(handler.captured_status, 200)
            self.assertEqual(handler.captured_payload["navKey"], "docs")
            self.assertIn("Docs", handler.captured_payload["content"])

    def test_admin_user_dashboard_keeps_user_shell_with_admin_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/dashboard"
            handler.get_dashboard()

            self.assertEqual(handler.captured_status, 200)
            self.assertNotIn('class="admin-body"', handler.captured_body)
            self.assertIn('class="app-layout"', handler.captured_body)
            self.assertIn('class="shell"', handler.captured_body)
            self.assertIn("管理后台", handler.captured_body)

    def test_static_cache_headers_allow_versioned_css_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            headers = app.static_cache_headers()

            self.assertEqual(headers["Cache-Control"], "public, max-age=604800, immutable")
            self.assertIn("Accept-Encoding", headers["Vary"])

    def test_payment_settings_make_wechat_and_alipay_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            app.update_payment_setting(
                con,
                "wechat",
                {
                    "enabled": "on",
                    "appid": "wx-app",
                    "mch_id": "mch-001",
                    "merchant_serial_no": "serial-001",
                    "notify_url": "https://node1.example/payment/wechat/notify",
                    "api_v3_key": "api-key",
                    "private_key_pem": "private-key",
                    "platform_cert_pem": "platform-cert",
                },
            )
            app.update_payment_setting(
                con,
                "alipay",
                {
                    "enabled": "on",
                    "app_id": "ali-app",
                    "gateway_url": "https://openapi.alipay.com/gateway.do",
                    "notify_url": "https://node1.example/payment/alipay/notify",
                    "return_url": "https://node1.example/dashboard",
                    "sign_type": "RSA2",
                    "merchant_private_key": "merchant-private",
                    "alipay_public_key": "alipay-public",
                },
            )
            settings = app.get_all_payment_settings(con)
            con.close()

            enabled = [p for p, row in settings.items() if app.payment_setting_ready(row)]
            html = app.payment_method_choices(enabled)

            self.assertIn('value="wechat"', html)
            self.assertIn('value="alipay"', html)
            self.assertIn("微信支付", html)
            self.assertIn("支付宝", html)

    def test_alipay_payment_creates_signed_wap_pay_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            private_key, alipay_public_key = self.make_rsa_key_pair()
            con = app.get_db()
            app.update_payment_setting(
                con,
                "alipay",
                {
                    "enabled": "on",
                    "app_id": "2021006152662156",
                    "gateway_url": "https://openapi.alipay.com/gateway.do",
                    "notify_url": "https://node1.example/payment/alipay/notify",
                    "return_url": "https://node1.example/dashboard",
                    "sign_type": "RSA2",
                    "merchant_private_key": private_key,
                    "alipay_public_key": alipay_public_key,
                },
            )
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return b"payment_method=alipay"

                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = f"/orders/{order_id}/pay"
            handler.user = {"id": user_id, "email": "buyer@example.com", "wallet_balance_cents": 0, "role": "customer"}
            handler.post_order_pay(f"/orders/{order_id}/pay")

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn('action="https://openapi.alipay.com/gateway.do"', body)
            self.assertIn('name="method" value="alipay.trade.wap.pay"', body)
            self.assertIn("QUICK_WAP_WAY", body)
            self.assertIn('name="sign"', body)
            self.assertIn("正在打开支付宝", body)
            self.assertNotIn("等待收款确认", body)
            con = app.get_db()
            tx_row = con.execute("select request_json from payment_transactions where order_id=?", (order_id,)).fetchone()
            con.close()
            request_payload = json.loads(tx_row["request_json"])
            biz_content = json.loads(request_payload["biz_content"])
            self.assertTrue(biz_content["subject"].startswith("Swallow Subscription "))
            self.assertTrue(biz_content["subject"].isascii())
            self.assertNotIn("燕子", biz_content["subject"])
            self.assertNotIn("轻量月付", biz_content["subject"])

    def test_alipay_defaults_use_payment_domain_and_return_hops_to_main_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"), public_payment_base="https://pay.example")
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            order = con.execute("select * from orders where id=?", (order_id,)).fetchone()
            tx = app.create_payment_transaction(con, order_id, "alipay", order["amount_cents"])
            con.close()

            params = app.build_alipay_wap_pay_params(
                order,
                tx,
                {"app_id": "2021006152662156", "merchant_private_key": self.make_rsa_key_pair()[0]},
            )

            self.assertEqual(params["notify_url"], "https://pay.example/payment/alipay/notify")
            self.assertEqual(params["return_url"], "https://pay.example/payment/alipay/return")

            class ReturnHandler(app.App):
                def redirect(self, path):
                    self.redirect_target = path

            handler = object.__new__(ReturnHandler)
            handler.get_payment_alipay_return()

            self.assertEqual(handler.redirect_target, app.PUBLIC_SUB_BASE.rstrip("/") + "/dashboard#orders")

    def test_alipay_notify_marks_transaction_paid_and_provisions_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            alipay_private_key, alipay_public_key = self.make_rsa_key_pair()
            merchant_private_key, _ = self.make_rsa_key_pair()
            con = app.get_db()
            app.update_payment_setting(
                con,
                "alipay",
                {
                    "enabled": "on",
                    "app_id": "2021006152662156",
                    "gateway_url": "https://openapi.alipay.com/gateway.do",
                    "notify_url": "https://node1.example/payment/alipay/notify",
                    "return_url": "https://node1.example/dashboard",
                    "sign_type": "RSA2",
                    "merchant_private_key": merchant_private_key,
                    "alipay_public_key": alipay_public_key,
                },
            )
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)
            tx = app.create_payment_transaction(con, order_id, "alipay", 1900)
            params = {
                "app_id": "2021006152662156",
                "trade_no": "202606272200140000000000001",
                "out_trade_no": tx["provider_order_no"],
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "19.00",
                "seller_id": "2088000000000000",
                "buyer_id": "2088123400000000",
                "gmt_payment": "2026-06-27 14:55:00",
                "sign_type": "RSA2",
            }
            params["sign"] = app.alipay_sign_params(params, alipay_private_key)
            body = urllib.parse.urlencode(params).encode()
            con.close()

            class NotifyHandler(app.App):
                def get_body(self):
                    return body

                def send_response(self, status):
                    self.captured_status = status

                def send_header(self, key, value):
                    self.headers_out.append((key, value))

                def end_headers(self):
                    self.headers_ended = True

            handler = object.__new__(NotifyHandler)
            handler.headers = {}
            handler.headers_out = []
            handler.wfile = io.BytesIO()
            handler.post_payment_alipay_notify()

            self.assertEqual(handler.captured_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"success")
            con = app.get_db()
            order = con.execute("select status,payment_ref,paid_at from orders where id=?", (order_id,)).fetchone()
            tx_row = con.execute("select status,response_json from payment_transactions where id=?", (tx["id"],)).fetchone()
            sub = con.execute("select * from subscriptions where order_id=?", (order_id,)).fetchone()
            con.close()
            self.assertEqual(order["status"], "provisioned")
            self.assertEqual(order["payment_ref"], tx["provider_order_no"])
            self.assertGreater(order["paid_at"], 0)
            self.assertEqual(tx_row["status"], "paid")
            self.assertIn("TRADE_SUCCESS", tx_row["response_json"])
            self.assertIsNotNone(sub)

    def test_checkout_mobile_card_uses_compact_payment_summary_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            private_key, alipay_public_key = self.make_rsa_key_pair()
            con = app.get_db()
            app.update_payment_setting(
                con,
                "wechat",
                {
                    "enabled": "on",
                    "appid": "wx-app",
                    "mch_id": "mch-001",
                    "merchant_serial_no": "serial-001",
                    "notify_url": "https://node1.example/payment/wechat/notify",
                    "api_v3_key": "api-key",
                    "private_key_pem": "private-key",
                    "platform_cert_pem": "platform-cert",
                },
            )
            app.update_payment_setting(
                con,
                "alipay",
                {
                    "enabled": "on",
                    "app_id": "ali-app",
                    "gateway_url": "https://openapi.alipay.com/gateway.do",
                    "notify_url": "https://node1.example/payment/alipay/notify",
                    "return_url": "https://node1.example/dashboard",
                    "sign_type": "RSA2",
                    "merchant_private_key": private_key,
                    "alipay_public_key": alipay_public_key,
                },
            )
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("style.css?v=docs-simple-20260630", body)
            self.assertIn("checkout-card", body)
            self.assertIn("checkout-plan-line", body)
            self.assertIn("checkout-summary-card", body)
            self.assertIn("checkout-summary-row", body)
            self.assertIn("checkout-total-bar", body)
            self.assertIn("checkout-payment", body)
            self.assertIn("checkout-submit", body)
            self.assertIn("checkout-action-row", body)
            self.assertIn("checkout-cancel", body)
            self.assertIn("取消", body)
            self.assertIn("继续支付", body)
            self.assertIn('data-pay-provider="wechat"', body)
            self.assertIn('data-pay-provider="alipay"', body)
            self.assertIn('data-pay-provider="manual"', body)
            self.assertIn("pay-icon-wechat", body)
            self.assertIn("pay-icon-alipay", body)
            self.assertIn("pay-icon-manual", body)
            self.assertNotIn("开通方式", body)
            self.assertLess(body.index("checkout-plan-line"), body.index("checkout-summary-card"))
            self.assertLess(body.index("checkout-summary-card"), body.index("checkout-total-bar"))
            self.assertLess(body.index("checkout-total-bar"), body.index("checkout-payment"))

            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("mobile-checkout-polish-20260626", css)
            self.assertIn(".checkout-card", css)
            self.assertIn(".checkout-summary-card", css)
            self.assertIn(".checkout-summary-row", css)
            self.assertIn(".checkout-action-row", css)
            self.assertIn(".checkout-total-bar", css)
            self.assertIn(".checkout-submit", css)
            self.assertIn("payment-method-icons-20260627", css)
            self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
            self.assertIn(".checkout-payment .pay-icon", css)

    def test_manual_payment_pending_page_uses_customer_facing_copy_and_dashboard_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            order = {
                "order_no": "OMANUAL",
                "payment_ref": "manual-review-test",
                "product_name": "冲浪 | 月付",
                "amount_cents": 1900,
            }

            body = app.payment_pending_page(order, "manual", None)

            self.assertIn("您的订单将由后台工作人员受理，受理完成后即开通网络套餐。", body)
            self.assertIn('href="/dashboard"', body)
            self.assertIn("查看套餐", body)
            self.assertIn("payment-primary-action", body)
            self.assertIn("payment-status-card", body)
            self.assertIn("payment-compact-details", body)
            self.assertNotIn("当前系统已记录支付意向", body)
            self.assertNotIn("管理员可在订单管理中点击", body)
            self.assertNotIn("返回用户中心", body)

    def test_desktop_web_polish_is_scoped_without_removing_mobile_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = "/dashboard"
            handler.render("用户中心", "<section>Dashboard</section>")

            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("style.css?v=docs-simple-20260630", handler.captured_body)
            self.assertIn("desktop-web-polish-20260627", css)
            self.assertIn("docs-simple-20260630", css)
            self.assertIn("@media (min-width: 721px)", css)
            self.assertIn("mobile-checkout-polish-20260626", css)
            self.assertIn("final-mobile-bottom-tabs-20260626", css)
            self.assertIn(".user-checkout-card", css)
            self.assertIn(".desktop-sub-stats", css)

    def test_desktop_subscription_card_uses_plan_usage_device_and_expiry_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            sub = {
                "sub_id": "uDesk",
                "uuid": "00000000-0000-4000-8000-000000000000",
                "product_name": "轻量月付",
                "traffic_bytes": 80 * 1024**3,
                "device_limit": 5,
                "expires_at": 1784905174143,
                "xui_status": "local",
                "clash_url": "https://node1.example/clashx/uDesk",
            }

            card = app.subscription_card(sub)

            self.assertIn('<span class="desktop-sub-title">轻量月付</span>', card)
            self.assertIn("desktop-sub-stats", card)
            self.assertIn("desktop-sub-stat traffic", card)
            self.assertIn("0.00/80 GB", card)
            self.assertIn("desktop-sub-stat devices", card)
            self.assertIn("0/5", card)
            self.assertIn("desktop-sub-stat expiry", card)
            self.assertNotIn('<span class="desktop-sub-title">通用订阅</span>', card)

    def test_desktop_checkout_uses_compact_card_payment_grid_and_cancel_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            con.close()
            order_id = app.create_order_for_user(user_id, product_id, use_balance=False)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}
            handler.path = f"/checkout?id={order_id}"
            handler.get_checkout()

            body = handler.captured_body
            self.assertEqual(handler.captured_status, 200)
            self.assertIn("checkout-card user-checkout-card", body)
            self.assertIn("checkout-head", body)
            self.assertIn("checkout-summary-card", body)
            self.assertIn("checkout-action-row", body)
            self.assertNotIn("checkout-desktop", body)
            self.assertIn("取消", body)
            self.assertIn("继续支付", body)
            self.assertIn('action="/orders/', body)
            self.assertIn("/cancel", body)
            self.assertLess(body.index("checkout-summary-card"), body.index("checkout-payment"))

    def test_admin_subscription_rows_include_user_plan_xui_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,sort_order,created_at,updated_at)
                values('Light Plan','monthly',30,80,1900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OTEST',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    order_id,
                    product_id,
                    "user-1-uTest",
                    "uTest",
                    "00000000-0000-4000-8000-000000000000",
                    80 * 1024**3,
                    1784905174143,
                    "https://node1.example/clashx/uTest",
                    "https://node1.example/sub/uTest",
                    "local",
                    "OK",
                    app.now_ms(),
                ),
            )
            con.commit()
            row = app.admin_subscription_rows(con)
            con.close()

            self.assertIn("buyer@example.com", row)
            self.assertIn("Light Plan", row)
            self.assertIn("uTest", row)
            self.assertIn("user-1-uTest", row)
            self.assertIn("80 GB", row)
            self.assertIn('name="days"', row)
            self.assertIn(f'action="/admin/subscriptions/1/extend"', row)
            self.assertIn("补偿", row)

    def test_admin_can_extend_subscription_expiry_and_xui_client_by_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            old_expiry = 1784905174143
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            xui.execute(
                "insert into inbounds(id, settings) values(2, ?)",
                (json.dumps({"clients": [{"email": "user-1-uComp", "expiryTime": old_expiry, "enable": True}]}),),
            )
            xui.execute(
                "insert into clients(email,sub_id,uuid,flow,limit_ip,total_gb,expiry_time,enable,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                ("user-1-uComp", "uComp", "00000000-0000-4000-8000-000000000002", "xtls-rprx-vision", 5, 80 * 1024**3, old_expiry, 1, 1, 1),
            )
            xui.execute(
                "insert into client_traffics(inbound_id,enable,email,up,down,expiry_time,total,reset,last_online) values(?,?,?,?,?,?,?,?,?)",
                (2, 1, "user-1-uComp", 0, 0, old_expiry, 80 * 1024**3, 30, 1),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            product_id = con.execute("select id from products where name=? limit 1", ("轻量月付",)).fetchone()["id"]
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OCOMP',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            sub_id = con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,traffic_reset_days,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    order_id,
                    product_id,
                    "user-1-uComp",
                    "uComp",
                    "00000000-0000-4000-8000-000000000002",
                    80 * 1024**3,
                    30,
                    old_expiry,
                    "https://node1.example/clashx/uComp",
                    "https://node1.example/sub/uComp",
                    "local",
                    "OK",
                    5,
                    app.now_ms(),
                ),
            ).lastrowid
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def get_body(self):
                    return b"days=5&note=%E6%B5%8B%E8%AF%95%E8%A1%A5%E5%81%BF"

                def redirect(self, path):
                    self.redirect_path = path

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.user = {"id": admin_id, "email": "admin-test@example.com", "role": "admin"}
            handler.post_admin_subscription_extend(f"/admin/subscriptions/{sub_id}/extend")

            expected_expiry = old_expiry + 5 * 86400 * 1000
            con = app.get_db()
            sub = con.execute("select expires_at,xui_message from subscriptions where id=?", (sub_id,)).fetchone()
            adjustment = con.execute("select * from subscription_compensations where subscription_id=?", (sub_id,)).fetchone()
            con.close()
            self.assertEqual(handler.redirect_path, "/admin/subscriptions")
            self.assertEqual(sub["expires_at"], expected_expiry)
            self.assertIn("Extended by 5 days", sub["xui_message"])
            self.assertEqual(adjustment["days"], 5)
            self.assertEqual(adjustment["old_expires_at"], old_expiry)
            self.assertEqual(adjustment["new_expires_at"], expected_expiry)

            xui = sqlite3.connect(xui_db)
            xui.row_factory = sqlite3.Row
            inbound = xui.execute("select settings from inbounds where id=2").fetchone()
            client = xui.execute("select expiry_time from clients where email='user-1-uComp'").fetchone()
            traffic = xui.execute("select expiry_time from client_traffics where email='user-1-uComp'").fetchone()
            xui.close()
            settings = json.loads(inbound["settings"])
            settings_client = [c for c in settings["clients"] if c["email"] == "user-1-uComp"][0]
            self.assertEqual(settings_client["expiryTime"], expected_expiry)
            self.assertEqual(client["expiry_time"], expected_expiry)
            self.assertEqual(traffic["expiry_time"], expected_expiry)

    def test_admin_subscription_rows_include_device_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")
            con = app.get_db()
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,sort_order,created_at,updated_at)
                values('Family Plan','monthly',30,160,4,4900,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('ODEVROW',?,?,4900,'provisioned','manual',?,?,4900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    order_id,
                    product_id,
                    "user-1-uDevice",
                    "uDevice",
                    "00000000-0000-4000-8000-000000000001",
                    160 * 1024**3,
                    1784905174143,
                    "https://node1.example/clashx/uDevice",
                    "https://node1.example/sub/uDevice",
                    "local",
                    "OK",
                    4,
                    app.now_ms(),
                ),
            )
            con.commit()
            row = app.admin_subscription_rows(con)
            con.close()

            self.assertIn("0 / 4", row)
            self.assertIn("最后在线 -", row)

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            admin_id = app.get_db().execute(
                "select id from users where email=?", ("admin-test@example.com",)
            ).fetchone()["id"]
            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/subscriptions"
            handler.get_admin_subscriptions()
            self.assertIn("<th>设备使用</th>", handler.captured_body)

    def test_admin_nodes_page_renders_seeded_node_capacity_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/nodes"
            handler.get_admin_nodes()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("节点管理", handler.captured_body)
            self.assertIn("Node1 US Direct", handler.captured_body)
            self.assertIn("US", handler.captured_body)
            self.assertIn("REALITY", handler.captured_body)
            self.assertIn("active", handler.captured_body)

    def test_fair_use_classifies_unlimited_high_consumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table client_traffics (
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            xui.execute(
                "insert into client_traffics(inbound_id,enable,email,up,down,total) values(?,?,?,?,?,?)",
                (2, 1, "user-1-uHeavy", 100 * 1024**3, 1150 * 1024**3, 0),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            user_id = app.create_customer_user("heavy@example.com", "password123")
            con = app.get_db()
            product_id = con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量月付','subscription','unlimited','monthly',30,0,5,2900,1,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OHEAVY',?,?,2900,'provisioned','manual',?,?,2900)
                """,
                (user_id, product_id, app.now_ms(), app.now_ms()),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,traffic_reset_days,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    order_id,
                    product_id,
                    "user-1-uHeavy",
                    "uHeavy",
                    "00000000-0000-4000-8000-000000000004",
                    0,
                    0,
                    1784905174143,
                    "https://node1.example/clashx/uHeavy",
                    "https://node1.example/sub/uHeavy",
                    "local",
                    "OK",
                    5,
                    app.now_ms(),
                ),
            )
            sub = con.execute("select s.*, p.product_family from subscriptions s join products p on p.id=s.product_id where s.sub_id='uHeavy'").fetchone()
            risk = app.subscription_fair_use_status(con, sub)
            con.close()

            self.assertEqual(risk["level"], "review")
            self.assertEqual(risk["used_gb"], 1250)
            self.assertIn("超过 1200 GB", risk["label"])

    def test_admin_subscriptions_search_and_risk_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table client_traffics (
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                create table inbound_client_ips (
                    id integer primary key autoincrement,
                    client_email text,
                    ips text
                );
                """
            )
            xui.execute(
                "insert into client_traffics(inbound_id,enable,email,up,down,total,last_online) values(?,?,?,?,?,?,?)",
                (2, 1, "user-1-uRisk", 0, 1300 * 1024**3, 0, 1784905174000),
            )
            xui.execute(
                "insert into inbound_client_ips(client_email,ips) values(?,?)",
                ("user-1-uRisk", '[{"ip":"223.104.78.196"},{"ip":"223.104.88.248"},{"ip":"223.104.78.196"},{"ip":"27.47.33.167"}]'),
            )
            xui.execute(
                "insert into client_traffics(inbound_id,enable,email,up,down,total) values(?,?,?,?,?,?)",
                (2, 1, "user-2-uNormal", 0, 20 * 1024**3, 0),
            )
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            con = app.get_db()
            admin_id = con.execute("select id from users where email=?", ("admin-test@example.com",)).fetchone()["id"]
            risk_user = app.create_customer_user("risk@example.com", "password123")
            normal_user = app.create_customer_user("normal@example.com", "password123")
            product_id = con.execute(
                """
                insert into products(name,product_type,product_family,billing_period,duration_days,traffic_gb,device_limit,price_cents,active,display_in_plans,sort_order,created_at,updated_at)
                values('不限量月付','subscription','unlimited','monthly',30,0,5,2900,1,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            for user_id, email_label, sub_id, order_no in [
                (risk_user, "user-1-uRisk", "uRisk", "ORISK"),
                (normal_user, "user-2-uNormal", "uNormal", "ONORMAL"),
            ]:
                order_id = con.execute(
                    """
                    insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                    values(?,?,?,2900,'provisioned','manual',?,?,2900)
                    """,
                    (order_no, user_id, product_id, app.now_ms(), app.now_ms()),
                ).lastrowid
                con.execute(
                    """
                    insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,traffic_reset_days,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        user_id,
                        order_id,
                        product_id,
                        email_label,
                        sub_id,
                        "00000000-0000-4000-8000-000000000005",
                        0,
                        0,
                        1784905174143,
                        f"https://node1.example/clashx/{sub_id}",
                        f"https://node1.example/sub/{sub_id}",
                        "local",
                        "OK",
                        5,
                        app.now_ms(),
                    ),
                )
            con.commit()
            con.close()

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(admin_id))}
            handler.path = "/admin/subscriptions?q=risk&risk=high"
            handler.get_admin_subscriptions()

            self.assertEqual(handler.captured_status, 200)
            self.assertIn("admin-subscriptions-page", handler.captured_body)
            self.assertIn("admin-filter-card", handler.captured_body)
            self.assertIn("admin-table-card", handler.captured_body)
            self.assertIn("admin-data-table", handler.captured_body)
            self.assertIn('name="q"', handler.captured_body)
            self.assertIn('name="risk"', handler.captured_body)
            self.assertIn("risk@example.com", handler.captured_body)
            self.assertIn("uRisk", handler.captured_body)
            self.assertIn("公平使用", handler.captured_body)
            self.assertIn("3 / 5", handler.captured_body)
            self.assertIn("最后在线 2026-07-24 22:59:34", handler.captured_body)
            self.assertNotIn("normal@example.com", handler.captured_body)

    def test_admin_subscription_empty_row_matches_device_limit_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            con = app.get_db()
            rows = app.admin_subscription_rows(con)
            con.close()

            self.assertIn("colspan='11'", rows)

    def test_xui_provision_writes_device_limit_to_client_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            con = sqlite3.connect(xui_db)
            con.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_inbounds (
                    client_id integer,
                    inbound_id integer,
                    flow_override text,
                    created_at integer,
                    unique(client_id, inbound_id)
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            con.execute("insert into inbounds(id, settings) values(2, ?)", (json.dumps({"clients": []}),))
            con.commit()
            con.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"

            result = app.provision_xui(
                "user-1-uLimited",
                "uLimited",
                "00000000-0000-4000-8000-000000000002",
                80 * 1024**3,
                1784905174143,
                4,
            )

            con = sqlite3.connect(xui_db)
            con.row_factory = sqlite3.Row
            inbound = con.execute("select settings from inbounds where id=2").fetchone()
            client = con.execute("select limit_ip from clients where email='user-1-uLimited'").fetchone()
            con.close()
            settings = json.loads(inbound["settings"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(settings["clients"][0]["limitIp"], 4)
            self.assertEqual(client["limit_ip"], 4)

    def test_periodic_products_reset_traffic_monthly_in_xui(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            con = sqlite3.connect(xui_db)
            con.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_inbounds (
                    client_id integer,
                    inbound_id integer,
                    flow_override text,
                    created_at integer,
                    unique(client_id, inbound_id)
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            con.execute("insert into inbounds(id, settings) values(2, ?)", (json.dumps({"clients": []}),))
            con.commit()
            con.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"

            result = app.provision_xui(
                "user-2-uMonthly",
                "uMonthly",
                "00000000-0000-4000-8000-000000000003",
                720 * 1024**3,
                1784905174143,
                10,
                30,
            )

            con = sqlite3.connect(xui_db)
            con.row_factory = sqlite3.Row
            inbound = con.execute("select settings from inbounds where id=2").fetchone()
            traffic = con.execute("select total, reset from client_traffics where email='user-2-uMonthly'").fetchone()
            con.close()
            settings = json.loads(inbound["settings"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(settings["clients"][0]["totalGB"], 720 * 1024**3)
            self.assertEqual(settings["clients"][0]["reset"], 30)
            self.assertEqual(traffic["total"], 720 * 1024**3)
            self.assertEqual(traffic["reset"], 30)

    def test_clash_config_can_use_origin_ip_with_domain_sni(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            app.NODE_CONNECT_HOST = "66.94.122.149"
            app.NODE_REALITY_SNI = "node1.talking202606.dpdns.org"

            config = app.build_clash_config({"sub_id": "uTest", "uuid": "00000000-0000-4000-8000-000000000000"})

            self.assertIn('server: "66.94.122.149"', config)
            self.assertIn('servername: "node1.talking202606.dpdns.org"', config)
            self.assertNotIn('server: "node1.talking202606.dpdns.org"', config)

    def test_xhttp_clash_config_matches_caddy_fronted_inbound(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            app.NODE_TRANSPORT = "xhttp"
            app.NODE_CONNECT_HOST = "node1.talking202606.dpdns.org"
            app.NODE_XHTTP_HOST = "node1.talking202606.dpdns.org"
            app.NODE_XHTTP_PATH = "/78f36abc92cc89fc"
            app.NODE_XHTTP_MODE = "auto"

            config = app.build_clash_config({"sub_id": "uTest", "uuid": "00000000-0000-4000-8000-000000000000"})

            self.assertIn('server: "node1.talking202606.dpdns.org"', config)
            self.assertIn("network: xhttp", config)
            self.assertIn("tls: true", config)
            self.assertIn('servername: "node1.talking202606.dpdns.org"', config)
            self.assertIn('path: "/78f36abc92cc89fc"', config)
            self.assertIn('host: "node1.talking202606.dpdns.org"', config)
            self.assertNotIn("reality-opts", config)
            self.assertNotIn("xtls-rprx-vision", config)

    def test_shadowrocket_node_url_is_vless_reality_single_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            app.NODE_CONNECT_HOST = "66.94.122.149"
            app.NODE_REALITY_SNI = "node1.talking202606.dpdns.org"
            app.NODE_REALITY_PUBLIC_KEY = "public-key-test"
            app.NODE_REALITY_SHORT_ID = "shortid"

            url = app.build_node_url({"sub_id": "uIos", "uuid": "00000000-0000-4000-8000-000000000000"})

            self.assertTrue(url.startswith("vless://00000000-0000-4000-8000-000000000000@66.94.122.149:443?"))
            self.assertIn("security=reality", url)
            self.assertIn("flow=xtls-rprx-vision", url)
            self.assertIn("sni=node1.talking202606.dpdns.org", url)
            self.assertIn("pbk=public-key-test", url)
            self.assertIn("#Node1-uIos", url)

    def test_shadowrocket_node_url_can_use_xhttp_tls(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            app.NODE_TRANSPORT = "xhttp"
            app.NODE_CONNECT_HOST = "node1.talking202606.dpdns.org"
            app.NODE_XHTTP_HOST = "node1.talking202606.dpdns.org"
            app.NODE_XHTTP_PATH = "/78f36abc92cc89fc"

            url = app.build_node_url({"sub_id": "uIos", "uuid": "00000000-0000-4000-8000-000000000000"})

            self.assertTrue(url.startswith("vless://00000000-0000-4000-8000-000000000000@node1.talking202606.dpdns.org:443?"))
            self.assertIn("security=tls", url)
            self.assertIn("type=xhttp", url)
            self.assertIn("host=node1.talking202606.dpdns.org", url)
            self.assertIn("path=%2F78f36abc92cc89fc", url)
            self.assertNotIn("security=reality", url)
            self.assertNotIn("xtls-rprx-vision", url)

    def test_xhttp_provision_uses_empty_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            con = sqlite3.connect(xui_db)
            con.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_inbounds (
                    client_id integer,
                    inbound_id integer,
                    flow_override text,
                    created_at integer,
                    unique(client_id, inbound_id)
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            con.execute("insert into inbounds(id, settings) values(1, ?)", (json.dumps({"clients": []}),))
            con.commit()
            con.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            app.XUI_INBOUND_ID = 1
            app.NODE_TRANSPORT = "xhttp"

            result = app.provision_xui(
                "user-1-uXhttp",
                "uXhttp",
                "00000000-0000-4000-8000-000000000004",
                80 * 1024**3,
                1784905174143,
                5,
                30,
            )

            con = sqlite3.connect(xui_db)
            con.row_factory = sqlite3.Row
            inbound = con.execute("select settings from inbounds where id=1").fetchone()
            client = con.execute("select flow from clients where email='user-1-uXhttp'").fetchone()
            flow_override = con.execute(
                "select flow_override from client_inbounds where inbound_id=1"
            ).fetchone()
            con.close()
            settings = json.loads(inbound["settings"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(settings["clients"][0]["flow"], "")
            self.assertEqual(client["flow"], "")
            self.assertEqual(flow_override["flow_override"], "")

    def test_xhttp_provision_moves_existing_traffic_row_to_active_inbound(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            con = sqlite3.connect(xui_db)
            con.executescript(
                """
                create table inbounds (
                    id integer primary key,
                    settings text not null
                );
                create table clients (
                    id integer primary key autoincrement,
                    email text unique,
                    sub_id text,
                    uuid text,
                    flow text,
                    limit_ip integer,
                    total_gb integer,
                    expiry_time integer,
                    enable integer,
                    created_at integer,
                    updated_at integer
                );
                create table client_inbounds (
                    client_id integer,
                    inbound_id integer,
                    flow_override text,
                    created_at integer,
                    unique(client_id, inbound_id)
                );
                create table client_traffics (
                    id integer primary key autoincrement,
                    inbound_id integer,
                    enable integer,
                    email text unique,
                    up integer,
                    down integer,
                    expiry_time integer,
                    total integer,
                    reset integer,
                    last_online integer
                );
                """
            )
            con.execute("insert into inbounds(id, settings) values(1, ?)", (json.dumps({"clients": []}),))
            con.execute(
                """
                insert into client_traffics(inbound_id,enable,email,up,down,expiry_time,total,reset,last_online)
                values(2,1,'user-1-uMove',123,456,1000,789,30,999)
                """
            )
            con.commit()
            con.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            app.XUI_INBOUND_ID = 1
            app.NODE_TRANSPORT = "xhttp"

            result = app.provision_xui(
                "user-1-uMove",
                "uMove",
                "00000000-0000-4000-8000-000000000005",
                80 * 1024**3,
                1784905174143,
                5,
                30,
            )

            con = sqlite3.connect(xui_db)
            con.row_factory = sqlite3.Row
            rows = con.execute("select * from client_traffics where email='user-1-uMove'").fetchall()
            con.close()
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["inbound_id"], 1)
            self.assertEqual(rows[0]["up"], 123)
            self.assertEqual(rows[0]["down"], 456)
            self.assertEqual(rows[0]["last_online"], 999)

    def test_public_pages_explain_ios_shadowrocket_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {}
            handler.path = "/plans"
            handler.get_plans()
            plans_html = handler.captured_body

            handler.path = "/docs"
            handler.get_docs()
            docs_html = handler.captured_body

            self.assertIn("支持 iOS/ Android/ MacOS/ Windows全平台", plans_html)
            self.assertIn("Shadowrocket", docs_html)
            self.assertIn("iOS", docs_html)
            self.assertIn("复制 Shadowrocket 节点", docs_html)

    def test_share_helpers_create_safe_invite_and_plan_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            invite_url = "https://node1.example/register?invite=ABCD1234"
            invite_text = app.build_invite_share_text(invite_url)

            self.assertIn(invite_url, invite_text)
            self.assertIn("Node1", invite_text)
            self.assertNotIn("@", invite_text)

            plan = {"id": 9, "name": "Light Plan", "duration_days": 30, "traffic_gb": 80, "price_cents": 1900}
            plan_url = app.build_plan_share_url(plan)
            plan_text = app.build_plan_share_text(plan, plan_url)

            self.assertEqual(plan_url, app.PUBLIC_SUB_BASE.rstrip("/") + "/plans?plan=9")
            self.assertIn("Light Plan", plan_text)
            self.assertIn("80 GB", plan_text)
            self.assertIn(plan_url, plan_text)
            self.assertNotIn("password", plan_text.lower())

    def test_share_panel_supports_copy_telegram_and_wechat_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            text = "Node1 Light Plan\nhttps://node1.example/plans?plan=9"
            url = "https://node1.example/plans?plan=9"

            panel = app.share_panel("plan-9", "分享套餐", url, text)
            telegram_url = app.telegram_share_url(text, url)

            self.assertIn('data-share-panel="plan-9"', panel)
            self.assertIn("复制文案", panel)
            self.assertIn("复制链接", panel)
            self.assertIn("分享到 Telegram", panel)
            self.assertIn("发到微信", panel)
            self.assertIn("复制后粘贴到微信", panel)
            self.assertIn('class="share-sheet-close"', panel)
            self.assertIn('data-share-close', panel)
            self.assertIn(app.html.escape(telegram_url, quote=True), panel)
            self.assertIn("t.me/share/url", telegram_url)
            self.assertIn("Node1+Light+Plan", telegram_url)

            app_source = Path("app.py.affiliates").read_text(encoding="utf-8")
            css = Path("style.css.affiliates").read_text(encoding="utf-8")
            self.assertIn("data-share-close", app_source)
            self.assertIn("shareClose", app_source)
            self.assertIn(".share-sheet-close", css)

    def test_reconcile_replaced_subscriptions_keeps_only_latest_active_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            xui = sqlite3.connect(xui_db)
            xui.executescript(
                """
                create table client_traffics (
                    inbound_id integer,
                    enable integer,
                    email text
                );
                """
            )
            xui.execute("insert into client_traffics(inbound_id,enable,email) values(?,?,?)", (2, 1, "user-1-old"))
            xui.execute("insert into client_traffics(inbound_id,enable,email) values(?,?,?)", (2, 1, "user-1-new"))
            xui.commit()
            xui.close()

            app = load_app(os.path.join(tmp, "shop.db"), xui_db_path=xui_db)
            app.XUI_MODE = "local"
            user_id = app.create_customer_user("buyer@example.com", "password123")
            now = app.now_ms()
            con = app.get_db()
            product_id = con.execute("select id from products order by id limit 1").fetchone()["id"]
            old_order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('OOLD',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, product_id, now, now),
            ).lastrowid
            new_order_id = con.execute(
                """
                insert into orders(order_no,user_id,product_id,amount_cents,status,payment_method,created_at,updated_at,gross_amount_cents)
                values('ONEW',?,?,1900,'provisioned','manual',?,?,1900)
                """,
                (user_id, product_id, now + 1, now + 1),
            ).lastrowid
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (user_id, old_order_id, product_id, "user-1-old", "uOld", "00000000-0000-4000-8000-000000000001", 80 * 1024**3, now + 10000, "c", "u", "local", "OK", 1, now),
            )
            con.execute(
                """
                insert into subscriptions(user_id,order_id,product_id,email_label,sub_id,uuid,traffic_bytes,expires_at,clash_url,universal_url,xui_status,xui_message,device_limit,created_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (user_id, new_order_id, product_id, "user-1-new", "uNew", "00000000-0000-4000-8000-000000000002", 80 * 1024**3, now + 20000, "c", "u", "local", "OK", 5, now + 1),
            )
            con.commit()

            app.reconcile_replaced_subscriptions(con)
            con.commit()
            old_sub = con.execute("select revoked_at,xui_status,revoked_reason from subscriptions where email_label='user-1-old'").fetchone()
            new_sub = con.execute("select revoked_at,xui_status from subscriptions where email_label='user-1-new'").fetchone()
            con.close()

            xui = sqlite3.connect(xui_db)
            xui.row_factory = sqlite3.Row
            old_traffic = xui.execute("select enable from client_traffics where email='user-1-old'").fetchone()
            new_traffic = xui.execute("select enable from client_traffics where email='user-1-new'").fetchone()
            xui.close()

            self.assertGreater(old_sub["revoked_at"], 0)
            self.assertEqual(old_sub["xui_status"], "replaced")
            self.assertEqual(old_sub["revoked_reason"], "replaced")
            self.assertEqual(new_sub["revoked_at"], 0)
            self.assertEqual(new_sub["xui_status"], "local")
            self.assertEqual(old_traffic["enable"], 0)
            self.assertEqual(new_traffic["enable"], 1)

    def test_plan_and_invitation_pages_render_share_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            user_id = app.create_customer_user("buyer@example.com", "password123")

            class DummyHandler(app.App):
                def send_html(self, status, body, headers=None):
                    self.captured_status = status
                    self.captured_body = body

            handler = object.__new__(DummyHandler)
            handler.headers = {"Cookie": "sid=" + app.sign(str(user_id))}

            handler.path = "/plans"
            handler.get_plans()
            self.assertEqual(handler.captured_status, 200)
            self.assertNotIn('data-share-kind="plan"', handler.captured_body)
            self.assertNotIn("分享套餐", handler.captured_body)

            handler.path = "/invitations"
            handler.get_invitations()
            self.assertEqual(handler.captured_status, 200)
            self.assertIn('data-share-kind="invite"', handler.captured_body)
            self.assertIn("分享邀请", handler.captured_body)
            self.assertIn("/register?invite=", handler.captured_body)

    def test_dashboard_copy_actions_include_shadowrocket_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))
            sub = {
                "sub_id": "uIos",
                "uuid": "00000000-0000-4000-8000-000000000000",
                "traffic_bytes": 80 * 1024**3,
                "expires_at": 1784905174143,
                "xui_status": "local",
                "clash_url": "https://node1.example/clashx/uIos",
            }

            shortcut_html = app.shortcut_panel(sub)
            card_html = app.subscription_card(sub)

            self.assertIn("复制 Shadowrocket 节点", shortcut_html)
            self.assertIn("iOS Shadowrocket", shortcut_html)
            self.assertIn("复制 Shadowrocket 节点", card_html)
            self.assertIn("复制 Clash 订阅", card_html)

    def test_subscription_headers_report_xui_usage_total_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            xui_db = os.path.join(tmp, "x-ui.db")
            app_db = os.path.join(tmp, "shop.db")
            con = __import__("sqlite3").connect(xui_db)
            con.execute(
                """
                create table client_traffics (
                    inbound_id integer,
                    email text,
                    up integer,
                    down integer,
                    total integer,
                    expiry_time integer
                )
                """
            )
            con.execute(
                "insert into client_traffics(inbound_id,email,up,down,total,expiry_time) values(?,?,?,?,?,?)",
                (2, "user-5-uLoggTuCuWc", 123, 456, 2000, 1784905174143),
            )
            con.commit()
            con.close()

            app = load_app(app_db, xui_db_path=xui_db)
            sub = {
                "email_label": "user-5-uLoggTuCuWc",
                "traffic_bytes": 1000,
                "expires_at": 1784905174143,
            }

            headers = app.subscription_response_headers(sub)

            self.assertEqual(headers["Subscription-Userinfo"], "upload=123; download=456; total=1000; expire=1784905174")
            self.assertEqual(headers["Profile-Update-Interval"], "24")

    def test_invited_paid_order_awards_commission_on_net_paid_amount_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = load_app(os.path.join(tmp, "shop.db"))

            referrer_id = app.create_customer_user("referrer@example.com", "password123")
            con = app.get_db()
            invite_code = con.execute("select invite_code from users where id=?", (referrer_id,)).fetchone()["invite_code"]
            con.close()

            buyer_id = app.create_customer_user("buyer@example.com", "password123", invite_code=invite_code)
            app.set_site_setting("affiliate_commission_bps", "1000")
            app.adjust_wallet_balance(buyer_id, 2000, "admin_adjust", "test initial balance")

            con = app.get_db()
            product_id = con.execute(
                """
                insert into products(name,billing_period,duration_days,traffic_gb,price_cents,active,sort_order,created_at,updated_at)
                values('Test Plan','monthly',30,100,10000,1,1,?,?)
                """,
                (app.now_ms(), app.now_ms()),
            ).lastrowid
            con.commit()
            con.close()

            order_id = app.create_order_for_user(buyer_id, product_id, use_balance=True)
            con = app.get_db()
            order = con.execute("select * from orders where id=?", (order_id,)).fetchone()
            self.assertEqual(order["gross_amount_cents"], 10000)
            self.assertEqual(order["balance_discount_cents"], 2000)
            self.assertEqual(order["amount_cents"], 8000)
            con.execute(
                "update orders set status='paid', payment_ref='test-paid', paid_at=?, updated_at=? where id=?",
                (app.now_ms(), app.now_ms(), order_id),
            )
            con.commit()
            con.close()

            app.provision_order(order_id)
            app.provision_order(order_id)

            con = app.get_db()
            referrer = con.execute("select wallet_balance_cents from users where id=?", (referrer_id,)).fetchone()
            buyer = con.execute("select wallet_balance_cents from users where id=?", (buyer_id,)).fetchone()
            commissions = con.execute(
                "select * from wallet_ledger where user_id=? and entry_type='affiliate_commission'",
                (referrer_id,),
            ).fetchall()
            con.close()

            self.assertEqual(buyer["wallet_balance_cents"], 0)
            self.assertEqual(referrer["wallet_balance_cents"], 800)
            self.assertEqual(len(commissions), 1)
            self.assertEqual(commissions[0]["amount_cents"], 800)


if __name__ == "__main__":
    unittest.main()
