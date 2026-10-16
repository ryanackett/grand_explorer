"""Hardening checks: admin login throttling, the get-data GET fallback, and
escaping of database text at the get-data / data-cart HTML sinks."""
from __future__ import annotations

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()
TEMPLATES = Path(__file__).resolve().parent / "templates"


class AdminLoginTests(TestCase):
    def test_anonymous_admin_goes_to_rate_limited_allauth_login(self):
        resp = self.client.get("/admin/", follow=True)
        final_url, _ = resp.redirect_chain[-1]
        self.assertTrue(final_url.startswith(reverse("account_login")), final_url)
        self.assertIn("next=/admin/", final_url)

    def test_django_admin_login_form_is_not_served(self):
        resp = self.client.get(reverse("admin:login"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("account_login")))
        # Posting credentials straight at the admin form gets nowhere either.
        User.objects.create_superuser("root", email="root@university.edu", password="pw")
        resp = self.client.post(reverse("admin:login"), {"username": "root", "password": "pw"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signed_in_non_staff_gets_403_not_a_password_form(self):
        user = User.objects.create_user("u", email="u@university.edu", password="pw")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 403)

    def test_staff_still_reach_admin(self):
        admin_user = User.objects.create_superuser("a", email="a@university.edu", password="pw")
        self.client.force_login(admin_user)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


class ExplorerLoginTests(TestCase):
    def test_anonymous_explorer_goes_to_allauth_login_not_its_own_form(self):
        resp = self.client.get("/explorer/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("account_login")))
        self.assertIn("next=/explorer/", resp["Location"])

    def test_non_superuser_gets_403(self):
        staff = User.objects.create_user("s", email="s@university.edu", password="pw", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/explorer/").status_code, 403)

    def test_superuser_reaches_explorer(self):
        root = User.objects.create_superuser("r", email="r@university.edu", password="pw")
        self.client.force_login(root)
        self.assertEqual(self.client.get("/explorer/").status_code, 200)


class GetDataGetTests(TestCase):
    def test_plain_get_redirects_to_filter_picker(self):
        resp = self.client.get("/get-data/")
        self.assertRedirects(resp, reverse("query-filters"), fetch_redirect_response=False)


class HtmlSinkEscapingTests(TestCase):
    """Database text interpolated into innerHTML must go through escHtml()."""

    SINKS = {
        "get-data.html": ["displayTitle", "displaySite", "p.Citation", "t.name", "t.crop", "t.mgmt"],
        "data-cart.html": ["item.publication", "item.location", "item.name", "item.crop", "item.mgmt"],
    }

    def test_sinks_are_escaped(self):
        for template, exprs in self.SINKS.items():
            src = (TEMPLATES / template).read_text(encoding="utf-8")
            for expr in exprs:
                raw = re.compile(r"\$\{\s*" + re.escape(expr) + r"\s*\}")
                self.assertIsNone(raw.search(src), f"{template}: ${{{expr}}} is not escaped")
                self.assertIn(f"escHtml({expr})", src, f"{template}: {expr} not passed to escHtml")

    def test_helper_is_defined_in_head(self):
        src = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertLess(src.index("function escHtml"), src.index("</head>"))
