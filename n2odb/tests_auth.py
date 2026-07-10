"""Contributor authentication (django-allauth) — registration with mandatory email
verification, the gate that keeps unverified/fake accounts away from the login-gated
workbench (and the LLM)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from n2odb.models_upload import UploadSubmission


class AllauthFlowTests(TestCase):
    def test_login_and_signup_pages_render(self):
        self.assertEqual(self.client.get(reverse("account_login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("account_signup")).status_code, 200)

    def test_signup_sends_verification_and_does_not_log_in(self):
        resp = self.client.post(reverse("account_signup"), {
            "email": "ada@university.edu",
            "password1": "Str0ngPass!23",
            "password2": "Str0ngPass!23",
        })
        # Account exists…
        User = get_user_model()
        self.assertTrue(User.objects.filter(email="ada@university.edu").exists())
        # …a verification email went out…
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("confirm", mail.outbox[0].body.lower())
        # …but the user is NOT authenticated (verification is mandatory).
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(resp.status_code, 302)  # → "verification sent" page

    def test_unverified_login_is_blocked(self):
        User = get_user_model()
        User.objects.create_user("ada", email="ada@university.edu", password="Str0ngPass!23")
        self.client.post(reverse("account_login"),
                         {"login": "ada@university.edu", "password": "Str0ngPass!23"})
        # No session login: an unverified address can't authenticate.
        self.assertNotIn("_auth_user_id", self.client.session)


class WorkbenchAccessTests(TestCase):
    """The wizard is login-required and owner-scoped: anonymous → login, a non-owner →
    404 (the token never reveals a submission exists), owner/staff → access granted."""

    def _user(self, name, **kw):
        return get_user_model().objects.create_user(
            name, email=f"{name}@university.edu", password="pw", **kw)

    def test_anonymous_redirected_to_login(self):
        sub = UploadSubmission.objects.create()
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_non_owner_gets_404(self):
        sub = UploadSubmission.objects.create(owner=self._user("owner"))
        self.client.force_login(self._user("intruder"))
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 404)

    def test_owner_is_granted_access(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(owner=owner)  # no data → view redirects on
        self.client.force_login(owner)
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)          # not a 404/login: access allowed
        self.assertIn("submit-data", resp.url)

    def test_staff_can_access_any_submission(self):
        sub = UploadSubmission.objects.create(owner=self._user("owner"))
        self.client.force_login(self._user("curator", is_staff=True))
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("submit-data", resp.url)

    def test_my_submissions_hides_empty_drafts(self):
        owner = self._user("owner")
        UploadSubmission.objects.create(owner=owner)  # blank draft → hidden
        UploadSubmission.objects.create(owner=owner, metadata_values={
            "pub_title": {"value": "Real study", "source": "user"}})
        self.client.force_login(owner)
        body = self.client.get(reverse("my-submissions")).content.decode()
        self.assertIn("Real study", body)
        self.assertNotIn("(untitled draft)", body)  # the blank draft isn't listed

    def test_submit_data_reuses_empty_draft(self):
        owner = self._user("owner")
        UploadSubmission.objects.create(owner=owner)  # a blank draft already exists
        self.client.force_login(owner)
        self.client.get(reverse("submit-data"))       # fresh session, no token
        # Adopted the blank draft instead of spawning a second one.
        self.assertEqual(UploadSubmission.objects.filter(owner=owner).count(), 1)

    def test_resume_reopens_changes_requested(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(
            owner=owner, status="changes_requested",
            metadata_values={"pub_title": {"value": "Sent back", "source": "user"}})
        self.client.force_login(owner)
        resp = self.client.get(reverse("workbench-resume", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)          # → submit-data
        sub.refresh_from_db()
        self.assertEqual(sub.status, "draft")            # editable again

    def test_my_submissions_offers_edit_for_changes_requested(self):
        owner = self._user("owner")
        UploadSubmission.objects.create(
            owner=owner, status="changes_requested",
            metadata_values={"pub_title": {"value": "Sent back", "source": "user"}})
        self.client.force_login(owner)
        body = self.client.get(reverse("my-submissions")).content.decode()
        self.assertIn("Edit &amp; resubmit", body)

    def test_my_submissions_lists_only_own(self):
        owner = self._user("owner")
        UploadSubmission.objects.create(owner=owner, metadata_values={
            "pub_title": {"value": "Mine", "source": "user"}})
        UploadSubmission.objects.create(owner=self._user("other"), metadata_values={
            "pub_title": {"value": "Theirs", "source": "user"}})
        self.client.force_login(owner)
        body = self.client.get(reverse("my-submissions")).content.decode()
        self.assertIn("Mine", body)
        self.assertNotIn("Theirs", body)
