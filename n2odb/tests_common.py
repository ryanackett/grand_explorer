"""Shared test helpers for the (now login-required) upload workbench.

Every contributor wizard endpoint is ``@login_required`` with an owner-or-staff check.
``AuthedWorkbenchTestCase`` logs in a **staff** user in setUp: staff bypasses the owner
check, so existing fixtures that create ownerless submissions keep working without
stamping an owner on each one. Dedicated auth/owner tests (anonymous → login redirect,
non-owner → 404) live in tests_auth.py.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase


class AuthedWorkbenchTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="worker", email="worker@university.edu", password="pw", is_staff=True,
        )
        self.client.force_login(self.user)
