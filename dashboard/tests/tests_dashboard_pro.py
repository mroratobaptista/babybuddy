# -*- coding: utf-8 -*-
from datetime import timedelta

from django.test import TestCase
from django.test import Client as HttpClient
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from faker import Faker

from core.models import Child, Feeding, DiaperChange, Sleep
from dashboard import dashboard_pro


class DashboardProViewTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fake = Faker()
        call_command("migrate", verbosity=0)
        cls.c = HttpClient()
        fake_user = fake.simple_profile()
        cls.credentials = {
            "username": fake_user["username"],
            "password": fake.password(),
        }
        cls.user = get_user_model().objects.create_user(
            is_superuser=True, **cls.credentials
        )
        cls.c.force_login(cls.user)
        call_command("fake", verbosity=0, children=1, days=7)
        cls.child = Child.objects.first()

    def _url(self, query=""):
        return "/children/{}/dashboard/pro/{}".format(self.child.slug, query)

    def test_renders_all_periods(self):
        for query in [
            "",
            "?period=day",
            "?period=yesterday",
            "?period=3days",
            "?period=week",
            "?period=lastweek",
            "?period=month",
            "?period=lastmonth",
            "?period=all",
        ]:
            page = self.c.get(self._url(query))
            self.assertEqual(page.status_code, 200, msg=query)
            self.assertTemplateUsed(page, "dashboard/child_pro.html")

    def test_invalid_params_fall_back(self):
        page = self.c.get(self._url("?period=bogus"))
        self.assertEqual(page.status_code, 200)

    def test_classic_dashboard_still_works(self):
        page = self.c.get("/children/{}/dashboard/".format(self.child.slug))
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "dashboard/child.html")


class PeriodWindowTestCase(TestCase):
    def test_day_is_live_today(self):
        window = dashboard_pro.get_period_window("day")
        self.assertEqual(window["period"], "day")
        self.assertTrue(window["is_live"])
        self.assertEqual(window["start_date"], timezone.localdate())

    def test_all_has_no_start_and_not_live(self):
        window = dashboard_pro.get_period_window("all")
        self.assertEqual(window["period"], "all")
        self.assertIsNone(window["start"])
        self.assertFalse(window["is_live"])

    def test_bad_period_defaults_to_day(self):
        window = dashboard_pro.get_period_window("nonsense")
        self.assertEqual(window["period"], "day")

    def test_yesterday_is_bounded_and_not_live(self):
        window = dashboard_pro.get_period_window("yesterday")
        yesterday = timezone.localdate() - timedelta(days=1)
        self.assertFalse(window["is_live"])
        self.assertEqual(window["start_date"], yesterday)
        self.assertEqual(window["end_date"], yesterday)

    def test_lastweek_ends_before_this_week(self):
        window = dashboard_pro.get_period_window("lastweek")
        today = timezone.localdate()
        this_monday = today - timedelta(days=today.weekday())
        self.assertFalse(window["is_live"])
        self.assertLess(window["end_date"], this_monday)
        # A full 7-day span (Mon..Sun).
        self.assertEqual((window["end_date"] - window["start_date"]).days, 6)

    def test_all_periods_are_resolvable(self):
        for period in dashboard_pro.PERIODS:
            window = dashboard_pro.get_period_window(period)
            self.assertEqual(window["period"], period)


class PredictionTestCase(TestCase):
    def setUp(self):
        self.child = Child.objects.create(
            first_name="Pred", last_name="Ictor", birth_date="2024-01-01"
        )
        self.now = timezone.localtime()

    def test_feeding_prediction_adds_average_interval(self):
        # Three feedings, two hours apart: last at now-1h.
        for hours_ago in (5, 3, 1):
            start = self.now - timedelta(hours=hours_ago)
            Feeding.objects.create(
                child=self.child,
                start=start,
                end=start + timedelta(minutes=15),
                type="breast milk",
                method="left breast",
            )
        prediction = dashboard_pro._feeding_prediction(self.child, self.now)
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction["used"], "day")
        # Average start-to-start interval is 2 hours.
        self.assertAlmostEqual(
            prediction["averages"]["day"].total_seconds(),
            timedelta(hours=2).total_seconds(),
            delta=1,
        )
        # Predicted next feeding ~= last start (now-1h) + 2h = now+1h.
        expected = self.now - timedelta(hours=1) + timedelta(hours=2)
        self.assertAlmostEqual(
            prediction["predicted"].timestamp(), expected.timestamp(), delta=2
        )
        self.assertFalse(prediction["is_late"])

    def test_diaper_prediction_flags_late(self):
        # Changes every 2h; last one 3h ago -> next predicted 1h in the past.
        for hours_ago in (7, 5, 3):
            DiaperChange.objects.create(
                child=self.child,
                time=self.now - timedelta(hours=hours_ago),
                wet=True,
                solid=False,
            )
        prediction = dashboard_pro._diaper_prediction(self.child, self.now)
        self.assertIsNotNone(prediction)
        self.assertTrue(prediction["is_late"])

    def test_no_data_no_prediction(self):
        self.assertIsNone(dashboard_pro._feeding_prediction(self.child, self.now))
        self.assertIsNone(dashboard_pro._nap_prediction(self.child, self.now))

    def test_not_registered_lists_untracked_types(self):
        Feeding.objects.create(
            child=self.child,
            start=self.now - timedelta(hours=1),
            end=self.now - timedelta(minutes=45),
            type="breast milk",
            method="left breast",
        )
        missing = {m["key"] for m in dashboard_pro._not_registered(self.child)}
        self.assertNotIn("feeding", missing)
        self.assertIn("diaperchange", missing)
        self.assertIn("temperature", missing)
