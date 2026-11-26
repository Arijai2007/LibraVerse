from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from apps.books.models import Book, IssuedBook
from apps.books.services import issue_book, return_book
from apps.core.models import Library
from apps.members.models import LibraryMembership
from apps.students.models import StudentExtra


class LibraryPassTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner")
        cls.librarian = User.objects.create_user("librarian", password="Library-pass-test")
        cls.reader = User.objects.create_user("reader")
        cls.outsider = User.objects.create_user("other-library-owner")
        cls.library = Library.objects.create(name="Main library", owner=cls.owner)
        Library.objects.create(name="Other library", owner=cls.outsider)
        for user, role in [(cls.librarian, "librarian"), (cls.reader, "member")]:
            LibraryMembership.objects.create(
                library=cls.library, user=user, role=role, must_change_password=False
            )
        cls.student = StudentExtra.objects.create(
            library=cls.library, name="Library Pass Student", enrollment="S-17", user=cls.reader
        )
        cls.book = Book.objects.create(
            library=cls.library, name="A Record of Stars", author="A. Writer", quantity=1
        )

    def pass_url(self, student=None):
        entry = reverse("student_pass", args=[(student or self.student).pk])
        response = self.client.get(entry)
        self.assertEqual(response.status_code, 302)
        return response.url

    def test_pass_and_qr_require_staff_in_the_students_library(self):
        self.client.force_login(self.librarian)
        url = self.pass_url()
        token = resolve(url).kwargs["token"]
        qr = reverse("student_pass_qr", args=[token])
        self.assertContains(self.client.get(url), self.student.name)
        self.assertContains(self.client.get(qr), "<svg")
        self.assertEqual(self.client.get(url.replace(token, token + "a")).status_code, 404)

        self.client.logout()
        login = self.client.get(url).url
        self.assertIn("next=", login)
        self.assertRedirects(
            self.client.post(
                login, {"username": self.librarian.username, "password": "Library-pass-test"}
            ),
            url,
        )
        self.client.logout()
        for user, status in [(self.reader, 403), (self.outsider, 404)]:
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertEqual(self.client.get(url).status_code, status)
                self.assertEqual(self.client.post(url).status_code, status)
                self.assertEqual(self.client.get(qr).status_code, status)
                self.client.logout()

    def test_scanned_pass_can_open_staff_workspace_after_sign_in_to_another_library(self):
        other_library = Library.objects.get(owner=self.outsider)
        LibraryMembership.objects.create(
            library=other_library, user=self.librarian, role="member",
            must_change_password=False,
        )
        self.client.force_login(self.owner)
        url = self.pass_url()
        self.client.logout()

        login = reverse("staff_login") + "?" + urlencode({"next": url})
        self.assertRedirects(self.client.get(url), login, fetch_redirect_response=False)
        self.assertContains(
            self.client.post(
                login, {"username": self.librarian.username, "password": "Library-pass-test"}
            ),
            other_library.name,
        )
        self.assertRedirects(
            self.client.post(login, {"selected_library": other_library.pk}),
            url,
            fetch_redirect_response=False,
        )
        prompt = self.client.get(url)
        self.assertContains(prompt, "Open library workspace")
        self.assertContains(prompt, self.library.name)
        self.assertNotContains(prompt, self.student.name)
        self.assertEqual(self.client.session["current_library_id"], other_library.pk)

        self.assertRedirects(self.client.post(url), url)
        self.assertEqual(self.client.session["current_library_id"], self.library.pk)
        self.assertContains(self.client.get(url), self.student.name)
        token = resolve(url).kwargs["token"]
        self.assertEqual(
            self.client.get(reverse("student_pass_qr", args=[token]))["Content-Type"],
            "image/svg+xml",
        )

    def test_fine_accrues_freezes_on_return_and_can_only_be_settled_once(self):
        policy = reverse("fine_policy")
        self.client.force_login(self.librarian)
        self.assertEqual(self.client.post(policy, {"fine_daily_rate": 5}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.post(policy, {"fine_daily_rate": "5.00", "fine_grace_days": 2}),
            policy,
        )
        due = timezone.localdate()
        loan = issue_book(None, self.book, due, borrower=self.reader)
        loan.refresh_from_db()
        self.assertEqual(loan.overdue_fine(due + timedelta(days=2)), Decimal("0"))
        self.client.post(policy, {"fine_daily_rate": "10.00", "fine_grace_days": 0})

        url = self.pass_url()
        token = resolve(url).kwargs["token"]
        settle = reverse("student_fine_action", args=[token, loan.pk])
        late = timezone.make_aware(datetime.combine(due + timedelta(days=5), time(12)))
        with patch("django.utils.timezone.now", return_value=late):
            self.assertContains(self.client.get(url), "₹15.00")
            self.assertEqual(self.client.get(settle).status_code, 405)
            self.assertContains(
                self.client.post(settle, {"action": "paid"}, follow=True),
                "Only returned loans with a fine can be settled.",
            )
            return_book(loan.pk, self.library, actor=self.owner)
        loan.refresh_from_db()
        self.assertEqual(loan.overdue_fine(due + timedelta(days=10)), Decimal("15"))

        other = StudentExtra.objects.create(library=self.library, enrollment="S-18")
        wrong_token = resolve(self.pass_url(other)).kwargs["token"]
        wrong_url = reverse("student_fine_action", args=[wrong_token, loan.pk])
        self.assertEqual(self.client.post(wrong_url, {"action": "paid"}).status_code, 404)
        self.client.force_login(self.librarian)
        self.assertContains(
            self.client.post(settle, {"action": "paid"}, follow=True), "Fine status updated."
        )
        self.assertContains(
            self.client.post(settle, {"action": "waived"}, follow=True),
            "This fine has already been settled.",
        )
        loan.refresh_from_db()
        self.assertEqual(loan.fine_status, IssuedBook.FineStatus.PAID)

    def test_staff_can_waive_an_offline_student_fine(self):
        self.client.force_login(self.librarian)
        self.student.user = None
        self.student.save(update_fields=["user"])
        self.library.fine_daily_rate = Decimal("5.00")
        self.library.save(update_fields=["fine_daily_rate"])
        due = timezone.localdate()
        loan = issue_book(self.student, self.book, due)
        later = timezone.make_aware(datetime.combine(due + timedelta(days=1), time(12)))
        with patch("django.utils.timezone.now", return_value=later):
            return_book(loan.pk, self.library, actor=self.librarian)
        url = self.pass_url()
        action = reverse("student_fine_action", args=[resolve(url).kwargs["token"], loan.pk])
        self.assertRedirects(self.client.post(action, {"action": "waived"}), url)
        loan.refresh_from_db()
        self.assertEqual(loan.fine_status, IssuedBook.FineStatus.WAIVED)
        self.assertEqual(loan.overdue_fine(), Decimal("5"))
