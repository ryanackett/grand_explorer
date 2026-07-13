"""My Account: name capture at signup, name editing, and the stable account identifier.

Email/password *changes* themselves are allauth's hardened views (reverification, current
-password required) — exercised by allauth's own suite, not re-tested here. These tests
cover the parts we added around them.
"""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class SignupNameTests(TestCase):
    def test_signup_captures_first_and_last_name(self):
        # A distinct address from other suites: allauth's per-email send rate limit lives
        # in the process cache and isn't rolled back between tests.
        resp = self.client.post(reverse("account_signup"), {
            "email": "namecapture@university.edu",
            "password1": "Str0ngPass!23",
            "password2": "Str0ngPass!23",
            "first_name": "Ada",
            "last_name": "Lovelace",
        })
        self.assertEqual(resp.status_code, 302)  # → "verification sent"
        u = User.objects.get(email="namecapture@university.edu")
        self.assertEqual((u.first_name, u.last_name), ("Ada", "Lovelace"))

    def test_signup_requires_names(self):
        resp = self.client.post(reverse("account_signup"), {
            "email": "nobody@university.edu",
            "password1": "Str0ngPass!23",
            "password2": "Str0ngPass!23",
        })
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors, not created
        self.assertFalse(User.objects.filter(email="nobody@university.edu").exists())


class PublicIdTests(TestCase):
    def test_public_id_is_set_unique_and_stable_across_email_change(self):
        a = User.objects.create_user("a", email="a@university.edu", password="pw")
        b = User.objects.create_user("b", email="b@university.edu", password="pw")
        self.assertIsInstance(a.public_id, uuid.UUID)
        self.assertNotEqual(a.public_id, b.public_id)

        original = a.public_id
        a.email = "a-new@university.edu"
        a.save()
        a.refresh_from_db()
        self.assertEqual(a.public_id, original)  # identifier survives the email change


class AccountSettingsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "worker", email="worker@university.edu", password="pw",
            first_name="Grace", last_name="Hopper")

    def test_requires_login(self):
        resp = self.client.get(reverse("account-settings"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_renders_details_and_links(self):
        self.client.force_login(self.user)
        body = self.client.get(reverse("account-settings")).content.decode()
        self.assertIn("Grace Hopper", body)                    # name shown read-only
        self.assertNotIn(str(self.user.public_id), body)       # account ID not surfaced
        self.assertIn(reverse("account-name"), body)           # change-name link
        self.assertIn(reverse("account_email"), body)          # change-email link
        self.assertIn(reverse("account_change_password"), body)  # change-password link

    def test_updates_name(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("account-name"),
                                {"first_name": "Ada", "last_name": "Lovelace"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("account-settings"))
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Ada", "Lovelace"))

    def test_blank_name_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("account-name"),
                                {"first_name": "", "last_name": ""})
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Grace")  # unchanged

    def test_change_name_page_requires_login(self):
        resp = self.client.get(reverse("account-name"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)


class LeaderboardTests(TestCase):
    def test_ranks_contributors_by_dataset_count_excluding_scraped(self):
        from n2odb.models import Publication
        top = User.objects.create_user("top", email="top@university.edu", password="pw",
                                       first_name="Top", last_name="One")
        second = User.objects.create_user("sec", email="sec@university.edu", password="pw",
                                          first_name="Sec", last_name="Two")
        for _ in range(3):
            Publication.objects.create(Contributor=top.public_id, PubTitle="x")
        Publication.objects.create(Contributor=second.public_id, PubTitle="y")
        Publication.objects.create(Contributor=None, PubTitle="scraped")  # legacy → excluded

        body = self.client.get(reverse("leaderboard")).content.decode()  # public page
        self.assertIn("Top One", body)
        self.assertIn("Sec Two", body)
        self.assertLess(body.index("Top One"), body.index("Sec Two"))  # 3 datasets ranks above 1

    def test_metrics_count_papers_fluxes_and_continents(self):
        import datetime
        from n2odb.models import (Publication, Experiment, Site, Treatment,
                                  RawMeasurementTreatment)
        u = User.objects.create_user("mira", email="mira@university.edu", password="pw",
                                     first_name="Mira", last_name="Bell")
        pub1 = Publication.objects.create(Contributor=u.public_id, PubTitle="p1")
        pub2 = Publication.objects.create(Contributor=u.public_id, PubTitle="p2")
        s_eu = Site.objects.create(Latitude=48.0, Longitude=2.0, Continent="Europe")
        s_na = Site.objects.create(Latitude=40.0, Longitude=-90.0, Continent="North America")
        e1 = Experiment.objects.create(PubID=pub1, SiteID=s_eu, ExperimentName="e1")
        e2 = Experiment.objects.create(PubID=pub2, SiteID=s_na, ExperimentName="e2")
        t1 = Treatment.objects.create(ExperimentID=e1, PrimaryCrop="maize")
        t2 = Treatment.objects.create(ExperimentID=e2, PrimaryCrop="wheat")
        d = datetime.date(2020, 1, 1)
        RawMeasurementTreatment.objects.create(treatmentid=t1, date=d, n2oflux=1.0)
        RawMeasurementTreatment.objects.create(treatmentid=t1, date=d, n2oflux=2.0)
        RawMeasurementTreatment.objects.create(treatmentid=t2, date=d, n2oflux=None)  # excluded
        RawMeasurementTreatment.objects.create(treatmentid=t2, date=d, n2oflux=3.0)

        body = self.client.get(reverse("leaderboard")).content.decode()
        self.assertIn('data-papers="2"', body)
        self.assertIn('data-fluxes="3"', body)       # 3 non-null fluxes; the null one excluded
        self.assertIn('data-continents="2"', body)   # Europe + North America

    def test_empty_state_when_no_contributions(self):
        body = self.client.get(reverse("leaderboard")).content.decode()
        self.assertIn("No contributions", body)

    def test_why_contribute_page_renders(self):
        resp = self.client.get(reverse("why-contribute"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Why it matters", resp.content.decode())  # a section heading
