"""Contributor authentication (django-allauth) — registration with mandatory email
verification, the gate that keeps unverified/fake accounts away from the login-gated
workbench (and the LLM)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from n2odb.models_upload import UploadSubmission


@override_settings(SITE_PASSWORD="secret", SITE_USERNAME="lab")
class SoftLaunchGateTests(TestCase):
    """The soft-launch HTTP Basic gate must block the general site but let
    allauth verification/reset links through — those open in a fresh browser
    that has never seen the gate credential."""

    def test_gate_blocks_normal_page(self):
        self.assertEqual(self.client.get("/about/").status_code, 401)

    def test_gate_exempts_accounts(self):
        # allauth pages have their own auth; the gate must not 401 them.
        self.assertNotEqual(self.client.get(reverse("account_login")).status_code, 401)


class AllauthFlowTests(TestCase):
    def test_login_and_signup_pages_render(self):
        self.assertEqual(self.client.get(reverse("account_login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("account_signup")).status_code, 200)

    def test_signup_sends_verification_and_does_not_log_in(self):
        resp = self.client.post(reverse("account_signup"), {
            "email": "ada@university.edu",
            "password1": "Str0ngPass!23",
            "password2": "Str0ngPass!23",
            "first_name": "Ada",
            "last_name": "Lovelace",
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
        self.assertEqual(resp.url, reverse("workbench-upload", args=[sub.public_token]))

    def test_staff_can_access_any_submission(self):
        sub = UploadSubmission.objects.create(owner=self._user("owner"))
        self.client.force_login(self._user("curator", is_staff=True))
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("workbench-upload", args=[sub.public_token]))

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

    def test_submit_data_starts_fresh_when_a_draft_is_ongoing(self):
        # The reported bug: with an unfinished submission in progress, "Start a new
        # submission" (and top-nav Contribute Data) resumed it instead of starting
        # fresh, making a second concurrent submission impossible.
        owner = self._user("owner")
        self.client.force_login(owner)
        self.client.get(reverse("submit-data"))       # draft A becomes the session draft
        first = UploadSubmission.objects.get(owner=owner)
        first.metadata_values = {"pub_title": {"value": "Ongoing", "source": "user"}}
        first.save()
        self.client.get(reverse("submit-data"))       # must NOT resume A
        subs = UploadSubmission.objects.filter(owner=owner)
        self.assertEqual(subs.count(), 2)             # A kept + a fresh draft B
        session_token = self.client.session.get("workbench_token")
        self.assertIsNotNone(session_token)
        self.assertNotEqual(str(first.public_token), session_token)

    def test_resume_reopens_changes_requested(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(
            owner=owner, status="changes_requested",
            metadata_values={"pub_title": {"value": "Sent back", "source": "user"}})
        self.client.force_login(owner)
        resp = self.client.get(reverse("workbench-resume", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)          # → this submission's upload page
        self.assertEqual(resp.url, reverse("workbench-upload", args=[sub.public_token]))
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

    def test_my_submissions_offers_discard_for_editable_only(self):
        owner = self._user("owner")
        draft = UploadSubmission.objects.create(owner=owner, status="draft", metadata_values={
            "pub_title": {"value": "Drafty", "source": "user"}})
        sent = UploadSubmission.objects.create(owner=owner, status="submitted", metadata_values={
            "pub_title": {"value": "Sent", "source": "user"}})
        self.client.force_login(owner)
        body = self.client.get(reverse("my-submissions")).content.decode()
        # Exactly one Discard control: the draft's. The submitted row has none.
        self.assertIn(reverse("workbench-discard", args=[draft.public_token]), body)
        self.assertNotIn(reverse("workbench-discard", args=[sent.public_token]), body)
        self.assertIn("Drafty", body)

    def test_discard_deletes_owned_draft(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(owner=owner, metadata_values={
            "pub_title": {"value": "Scrap me", "source": "user"}})
        self.client.force_login(owner)
        # Make it the session draft first: discarding must clear the pointer too.
        self.client.get(reverse("workbench-resume", args=[sub.public_token]))
        resp = self.client.post(reverse("workbench-discard", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("my-submissions"))
        self.assertFalse(UploadSubmission.objects.filter(pk=sub.pk).exists())
        self.assertNotIn("workbench_token", self.client.session)

    def test_discard_rejects_non_owner(self):
        sub = UploadSubmission.objects.create(owner=self._user("owner"), metadata_values={
            "pub_title": {"value": "Mine", "source": "user"}})
        self.client.force_login(self._user("intruder"))
        resp = self.client.post(reverse("workbench-discard", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 404)  # token never leaks the row exists
        self.assertTrue(UploadSubmission.objects.filter(pk=sub.pk).exists())

    def test_discard_rejects_submitted(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(owner=owner, status="submitted",
                                              metadata_values={
                                                  "pub_title": {"value": "Sent", "source": "user"}})
        self.client.force_login(owner)
        resp = self.client.post(reverse("workbench-discard", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 400)  # with a curator: not ours to throw away
        self.assertTrue(UploadSubmission.objects.filter(pk=sub.pk).exists())

    def test_discard_requires_post(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(owner=owner, metadata_values={
            "pub_title": {"value": "Mine", "source": "user"}})
        self.client.force_login(owner)
        self.assertEqual(
            self.client.get(reverse("workbench-discard", args=[sub.public_token])).status_code,
            405,
        )
        self.assertTrue(UploadSubmission.objects.filter(pk=sub.pk).exists())

    def test_workbench_upload_renders_owned_draft(self):
        owner = self._user("owner")
        sub = UploadSubmission.objects.create(owner=owner, metadata_values={
            "pub_title": {"value": "Mine", "source": "user"}})
        self.client.force_login(owner)
        resp = self.client.get(reverse("workbench-upload", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session.get("workbench_token"), str(sub.public_token))

    def test_workbench_upload_rejects_non_owner(self):
        sub = UploadSubmission.objects.create(owner=self._user("owner"))
        self.client.force_login(self._user("intruder"))
        resp = self.client.get(reverse("workbench-upload", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 404)


class ResumeStepTests(TestCase):
    """Resuming a submission lands on the furthest stage reached (capped at Review),
    not always the blank Upload page — but never past a step that needs a data table."""

    _SHEET = {"sheets": [{
        "name": "s.csv", "headers": ["date", "n2o_flux"],
        "rows": [["2018-05-01", "1.2"]],
        "dtypes": ["date", "float"],
        "n_rows": 1, "n_rows_stored": 1, "header_row": 0, "truncated": False,
    }]}

    def _step(self, **kw):
        from n2odb.views_workbench import _resume_step
        return _resume_step(UploadSubmission(**kw))

    def test_no_data_table_resumes_at_upload(self):
        # A saved answer but no parsed sheet: later stages redirect to Upload, so resume there.
        self.assertEqual(self._step(metadata_values={"pub_title": {"value": "x"}}), 1)

    def test_data_only_resumes_at_mapping(self):
        self.assertEqual(self._step(parsed_content=self._SHEET), 2)

    def test_mapping_done_resumes_at_site(self):
        self.assertEqual(
            self._step(parsed_content=self._SHEET,
                       mapping={"date": {"canonical_field": "date"}}), 3)

    def test_site_done_resumes_at_soil(self):
        self.assertEqual(
            self._step(parsed_content=self._SHEET,
                       metadata_values={"pub_title": {"value": "x"}}), 4)

    def test_timeline_done_caps_at_review(self):
        self.assertEqual(
            self._step(parsed_content=self._SHEET,
                       treatment_details={"treatments": ["A"]},
                       events={"global": {"planting": [{"fields": {}}]}}), 6)

    def test_resume_redirects_to_computed_stage(self):
        owner = get_user_model().objects.create_user(
            "owner", email="owner@university.edu", password="pw")
        sub = UploadSubmission.objects.create(
            owner=owner, parsed_content=self._SHEET,
            mapping={"date": {"canonical_field": "date"}})
        self.client.force_login(owner)
        resp = self.client.get(reverse("workbench-resume", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("workbench-site", args=[sub.public_token]))

    def test_rail_links_forward_to_reached_pages(self):
        # Fully progressed to Review, then viewing an *earlier* stage (Mapping): the rail
        # must still link forward to pages already visited. Timeline/Review URLs appear
        # only in the rail on the mapping page (the page's own nav only knows Site/next).
        owner = get_user_model().objects.create_user(
            "railfwd", email="railfwd@university.edu", password="pw")
        sub = UploadSubmission.objects.create(
            owner=owner, parsed_content=self._SHEET,
            mapping={"date": {"canonical_field": "date"}},
            metadata_values={"pub_title": {"value": "x"}},
            treatment_details={"treatments": ["A"]},
            events={"global": {"planting": [{"fields": {}}]}})
        self.client.force_login(owner)
        body = self.client.get(reverse("workbench-mapping", args=[sub.public_token])).content.decode()
        self.assertIn(reverse("workbench-timeline", args=[sub.public_token]), body)
        self.assertIn(reverse("workbench-review", args=[sub.public_token]), body)

    def test_rail_hides_unreached_pages(self):
        # Only data uploaded (furthest = Mapping): steps past it stay inert / unlinked.
        owner = get_user_model().objects.create_user(
            "railback", email="railback@university.edu", password="pw")
        sub = UploadSubmission.objects.create(owner=owner, parsed_content=self._SHEET)
        self.client.force_login(owner)
        body = self.client.get(reverse("workbench-mapping", args=[sub.public_token])).content.decode()
        self.assertNotIn(reverse("workbench-timeline", args=[sub.public_token]), body)
        self.assertNotIn(reverse("workbench-review", args=[sub.public_token]), body)
