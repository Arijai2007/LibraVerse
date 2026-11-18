import csv
from datetime import timedelta
from io import StringIO
from threading import Barrier, Thread

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, transaction
from django.test import (
    Client,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone

from apps.books.models import (
    ActivityLog,
    Book,
    BookCopy,
    BookHold,
    IssuedBook,
    Notification,
    Reservation,
)
from apps.books.services import (
    add_copies,
    cancel_reservation,
    initialize_title,
    issue_book,
    queue_position,
    reserve,
    return_book,
)
from apps.core.models import Library
from apps.members.models import LibraryMembership
from apps.students.models import StudentExtra


def make_library(owner):
    return Library.objects.create(
        name="Juniper Library", owner=owner, public_catalogue=True
    )


def add_member(library, username, role="member"):
    user = User.objects.create_user(username=username)
    LibraryMembership.objects.create(
        library=library, user=user, role=role, must_change_password=False
    )
    return user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CirculationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "owner", password="A-Decent-Library-Password-714"
        )
        cls.library = make_library(cls.owner)
        cls.librarian = add_member(cls.library, "desk", "librarian")
        cls.first = add_member(cls.library, "first")
        cls.second = add_member(cls.library, "second")
        cls.previous = add_member(cls.library, "previous_reader")
        cls.outsider = User.objects.create_user("outsider")
        cls.private = Library.objects.create(name="Private Library", owner=cls.outsider)
        cls.title = Book.objects.create(
            library=cls.library,
            name="A forest of books",
            author="S. K. Finch",
            quantity=1,
            category="Fiction",
            isbn="9780857197689",
            description="An illustrated collection.",
        )
        initialize_title(cls.title, cls.owner)
        cls.copy = cls.title.copies.get()
        cls.due = timezone.localdate() + timedelta(days=7)
        cls.old_loan = issue_book(None, cls.title, cls.due, borrower=cls.previous)

    def sign_in(self, user):
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse("dashboard")).status_code, 200)
        return client

    def test_registration_to_waitlist_to_pickup_and_reports(self):
        reader = Client()
        response = reader.post(
            reverse("register_member"),
            {
                "username": "new_reader",
                "library": self.library.code,
                "password1": "Silverbirch-Ink-794!",
                "password2": "Silverbirch-Ink-794!",
            },
        )
        self.assertRedirects(response, reverse("dashboard"))
        member = User.objects.get(username="new_reader")
        reader.post(reverse("logout"))
        self.assertRedirects(
            reader.post(
                reverse("adminlogin"),
                {
                    "username": member.username,
                    "password": "Silverbirch-Ink-794!",
                },
            ),
            reverse("dashboard"),
        )
        results = reader.get(
            reverse("catalogue"),
            {"q": "978-0-85719-768-9", "availability": "unavailable"},
        )
        self.assertContains(results, self.title.name)
        detail = reverse("book_detail", args=[self.title.pk])
        self.assertContains(reader.get(detail), "Join waitlist")
        endpoint = reverse("reservation_action", args=[self.title.pk])
        response = reader.post(
            endpoint,
            {"action": "reserve"},
            headers={"X-Libraverse-Fragment": "reservation"},
        )
        self.assertEqual(response.json(), {"message": "You joined the waitlist."})
        self.assertContains(reader.get(detail), "#1")
        self.assertEqual(
            reader.post(
                endpoint,
                {"action": "reserve"},
                headers={"X-Libraverse-Fragment": "reservation"},
            ).status_code,
            400,
        )
        librarian = self.sign_in(self.librarian)
        desk = reverse("circulation_desk")
        copy = reverse("copy_detail", args=[self.copy.code])
        self.assertRedirects(
            librarian.get(desk, {"code": self.copy.code.lower()}), copy
        )
        qr = librarian.get(reverse("copy_qr", args=[self.copy.code]))
        self.assertEqual(qr["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", qr.content)
        action = reverse("copy_action", args=[self.copy.code])
        self.assertRedirects(
            librarian.post(action, {"action": "return", "loan": self.old_loan.pk}), copy
        )
        self.assertContains(reader.get(detail), "Ready for pickup")
        self.assertContains(reader.get(reverse("notifications")), "is ready for pickup")
        self.assertContains(librarian.get(copy), member.username)
        librarian.post(
            action, {"action": "issue", "borrower": self.second.pk, "due": self.due}
        )
        self.assertFalse(IssuedBook.objects.filter(borrower=self.second).exists())
        self.assertRedirects(
            librarian.post(
                action, {"action": "issue", "borrower": member.pk, "due": self.due}
            ),
            copy,
        )
        loan = IssuedBook.objects.get(borrower=member)
        self.assertEqual(loan.copy, self.copy)
        self.assertEqual(
            Reservation.objects.get(book=self.title, member=member).status, "fulfilled"
        )
        self.assertContains(reader.get(reverse("dashboard")), self.title.name)
        filters = {
            "report": "active",
            "book": self.title.pk,
            "member": member.pk,
            "category": "Fiction",
        }
        self.assertContains(librarian.get(reverse("reports"), filters), self.copy.code)
        rows = list(
            csv.reader(
                StringIO(
                    librarian.get(
                        reverse("reports"), {**filters, "format": "csv"}
                    ).content.decode()
                )
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[1][:3], [self.title.name, member.username, self.copy.code]
        )
        pdf = librarian.get(reverse("reports"), {**filters, "format": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF-"))

    def test_fifo_expiry_cancellation_and_idempotent_return(self):
        first = reserve(self.title.pk, self.first)
        second = reserve(self.title.pk, self.second)
        self.assertEqual([queue_position(first), queue_position(second)], [1, 2])
        return_book(self.old_loan.pk, self.library)
        return_book(self.old_loan.pk, self.library)
        self.copy.refresh_from_db()
        self.title.refresh_from_db()
        self.assertEqual(self.copy.status, BookCopy.Status.HELD)
        self.assertEqual(self.title.quantity, 0)
        self.assertEqual(BookHold.objects.get(ended_at__isnull=True).reservation, first)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(queue_position(second), 1)
        with self.assertRaisesMessage(ValueError, "reserved"):
            issue_book(
                None, self.title, self.due, borrower=self.previous, copy_id=self.copy.pk
            )
        BookHold.objects.filter(reservation=first).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        call_command("process_circulation", stdout=StringIO())
        call_command("process_circulation", stdout=StringIO())
        first.refresh_from_db()
        self.assertEqual(first.status, "expired")
        self.assertEqual(
            BookHold.objects.get(ended_at__isnull=True).reservation, second
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.first, message__contains="expired"
            ).count(),
            1,
        )
        self.assertTrue(cancel_reservation(self.title.pk, self.second))
        self.copy.refresh_from_db()
        self.assertEqual(self.copy.status, "available")
        self.assertFalse(BookHold.objects.filter(ended_at__isnull=True).exists())

    def test_cancel_waiting_and_skip_ineligible_reader(self):
        reserve(self.title.pk, self.first)
        reserve(self.title.pk, self.second)
        reserve(self.title.pk, self.librarian)
        self.assertTrue(cancel_reservation(self.title.pk, self.first))
        self.second.is_active = False
        self.second.save(update_fields=["is_active"])
        return_book(self.old_loan.pk, self.library)
        self.assertEqual(
            Reservation.objects.get(member=self.second).status, "cancelled"
        )
        self.assertEqual(
            BookHold.objects.get(ended_at__isnull=True).reservation.member,
            self.librarian,
        )

    def test_release_hold_after_account_deletion(self):
        first = reserve(self.title.pk, self.first)
        second = reserve(self.title.pk, self.second)
        return_book(self.old_loan.pk, self.library)
        self.first.delete()
        call_command("process_circulation", stdout=StringIO())
        first.refresh_from_db()
        self.assertIsNone(first.member_id)
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(
            BookHold.objects.get(ended_at__isnull=True).reservation, second
        )

    def test_owner_invitation_and_role_assignment(self):
        owner = self.sign_in(self.owner)
        owner.post(
            reverse("add_member"),
            {
                "add_member": "1",
                "username": "invited",
                "email": "invited@example.com",
            },
        )
        membership = LibraryMembership.objects.get(
            library=self.library, user__username="invited"
        )
        self.assertFalse(membership.user.has_usable_password())
        member_actions = reverse("remove_member")
        response = owner.post(
            member_actions, {"action": "setup", "membership_id": membership.pk}
        )
        self.assertIn("no-store", response["Cache-Control"])
        signup = Client()
        response = signup.get(response.context["setup_url"])
        self.assertEqual(response.status_code, 302)
        self.assertContains(signup.get(response.url), "Set your password")
        self.assertRedirects(
            signup.post(
                response.url,
                {
                    "new_password1": "An-Invited-Reader-462!",
                    "new_password2": "An-Invited-Reader-462!",
                },
            ),
            reverse("adminlogin"),
        )
        membership.refresh_from_db()
        self.assertFalse(membership.must_change_password)
        owner.post(
            member_actions, {"action": "librarian", "membership_id": membership.pk}
        )
        self.assertEqual(
            self.sign_in(membership.user).get(reverse("inventory")).status_code, 200
        )

    def test_available_stock_reservation_and_prevent_double_borrow(self):
        add_copies(self.title.pk, self.library, 2, self.owner)
        entry = reserve(self.title.pk, self.first)
        self.assertEqual(entry.status, "held")
        loan = issue_book(None, self.title, self.due, borrower=self.first)
        with self.assertRaisesMessage(ValueError, "already have an open loan"):
            issue_book(None, self.title, self.due, borrower=self.first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            IssuedBook.objects.create(
                book=self.title, copy=loan.copy, borrower=self.second
            )
        return_book(loan.pk, self.library)
        loan.refresh_from_db()
        self.assertIsNotNone(loan.returned_at)
        self.assertTrue(
            ActivityLog.objects.filter(
                library=self.library, action="Copy returned"
            ).exists()
        )

    def test_due_and_overdue_notices_deduplicate_and_prevent_new_loan(self):
        IssuedBook.objects.filter(pk=self.old_loan.pk).update(
            return_date=timezone.localdate() + timedelta(days=1)
        )
        for _ in range(2):
            call_command("process_circulation", stdout=StringIO())
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.previous, event_key__contains=":due:"
            ).count(),
            1,
        )
        IssuedBook.objects.filter(pk=self.old_loan.pk).update(
            return_date=timezone.localdate() - timedelta(days=1)
        )
        for _ in range(2):
            call_command("process_circulation", stdout=StringIO())
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.previous, event_key__contains=":overdue:"
            ).count(),
            1,
        )
        other = Book.objects.create(
            library=self.library, name="Another title", author="S. K. Finch", quantity=1
        )
        with self.assertRaisesMessage(ValueError, "overdue"):
            reserve(other.pk, self.previous)

    def test_privacy_permissions_csrf_and_notification_ownership(self):
        private = Book.objects.create(
            library=self.private, name="Hidden title", author="Secret", quantity=1
        )
        initialize_title(private)
        anonymous = Client()
        self.assertEqual(
            anonymous.get(reverse("book_detail", args=[private.pk])).status_code, 404
        )
        self.assertEqual(
            anonymous.get(reverse("catalogue_suggestions"), {"q": "Hidden"}).json(),
            {"results": []},
        )
        member = self.sign_in(self.first)
        for name in (
            "inventory",
            "viewbook",
            "viewstudent",
            "reports",
            "circulation_desk",
        ):
            self.assertEqual(member.get(reverse(name)).status_code, 403)
        self.assertEqual(
            member.get(reverse("copy_qr", args=[self.copy.code])).status_code, 403
        )
        page = member.get(reverse("copy_detail", args=[self.copy.code]))
        self.assertContains(page, self.title.name)
        self.assertNotContains(page, self.previous.username)
        endpoint = reverse("copy_action", args=[self.copy.code])
        self.assertEqual(
            member.post(
                endpoint, {"action": "return", "loan": self.old_loan.pk}
            ).status_code,
            403,
        )
        staff = self.sign_in(self.librarian)
        private_copy = private.copies.get()
        self.assertEqual(
            staff.post(
                reverse("copy_action", args=[private_copy.code]), {"action": "status"}
            ).status_code,
            404,
        )
        self.assertEqual(
            staff.get(
                reverse("reports"),
                {"report": "active", "book": private.pk, "format": "csv"},
            ).status_code,
            400,
        )
        self.assertEqual(
            staff.post(reverse("catalogue_visibility"), {"public": "no"}).status_code,
            403,
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.first)
        self.assertEqual(
            csrf.post(
                reverse("reservation_action", args=[self.title.pk]),
                {"action": "reserve"},
            ).status_code,
            403,
        )
        reserve(self.title.pk, self.first)
        notice = Notification.objects.get(recipient=self.first)
        other = self.sign_in(self.second)
        self.assertEqual(
            other.post(reverse("mark_read"), {"notice": notice.pk}).status_code, 404
        )
        self.assertRedirects(
            member.post(reverse("mark_read"), {"notice": notice.pk}),
            reverse("notifications"),
        )
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)

    def test_inventory_edit_archive_and_restore(self):
        staff = self.sign_in(self.librarian)
        response = staff.post(
            reverse("addbook"),
            {
                "name": "New title",
                "author": "K. Alder",
                "language": "English",
                "category": "Essays",
                "isbn": "0-7475-3269-9",
                "quantity": 2,
            },
        )
        book = Book.objects.get(name="New title")
        detail = reverse("title_inventory", args=[book.pk])
        self.assertRedirects(response, detail)
        self.assertEqual(book.isbn, "0747532699")
        self.assertEqual(book.copies.count(), 2)
        self.assertContains(staff.get(detail), "Save title details")
        staff.post(
            detail,
            {
                "action": "edit",
                "name": "Renamed title",
                "author": "K. Alder",
                "language": "English",
                "category": "Essays",
                "isbn": book.isbn,
            },
        )
        book.refresh_from_db()
        self.assertEqual(book.name, "Renamed title")
        copy = book.copies.first()
        staff.post(
            reverse("copy_action", args=[copy.code]),
            {"action": "status", "status": "maintenance"},
        )
        copy.refresh_from_db()
        self.assertEqual(copy.status, "maintenance")
        staff.post(detail, {"action": "archive", "confirm": "yes"})
        self.assertEqual(
            Client().get(reverse("book_detail", args=[book.pk])).status_code, 404
        )
        staff.post(detail, {"action": "restore"})
        self.assertContains(
            Client().get(reverse("book_detail", args=[book.pk])), "Renamed title"
        )
        self.assertContains(
            staff.get(
                reverse("reports"), {"report": "inventory", "status": "maintenance"}
            ),
            copy.code,
        )

    def test_inactive_legacy_reader_cannot_lose_checkout_history(self):
        reader = StudentExtra.objects.create(
            library=self.library, name="Offline reader", enrollment="R-100"
        )
        add_copies(self.title.pk, self.library, 1, self.owner)
        issue_book(reader, self.title, self.due)
        staff = self.sign_in(self.librarian)
        staff.post(reverse("deletestudents"), {"selected_students": [reader.pk]})
        self.assertTrue(StudentExtra.objects.filter(pk=reader.pk).exists())


class ConcurrentCirculationTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_two_staff_checkouts_cannot_allocate_the_same_copy(self):
        owner = User.objects.create_user("owner")
        library = make_library(owner)
        book = Book.objects.create(
            library=library, name="Single copy", author="Finch", quantity=1
        )
        initialize_title(book)
        readers = [add_member(library, "alice"), add_member(library, "bob")]
        barrier = Barrier(2, timeout=10)
        results = []

        def attempt(reader_id):
            close_old_connections()
            try:
                title = Book.objects.get(pk=book.pk)
                reader = User.objects.get(pk=reader_id)
                barrier.wait()
                issue_book(None, title, timezone.localdate(), borrower=reader)
            except ValueError:
                results.append("unavailable")
            except Exception as error:
                results.append(repr(error))
            else:
                results.append("issued")
            finally:
                close_old_connections()

        threads = [Thread(target=attempt, args=[reader.pk]) for reader in readers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["issued", "unavailable"])
        self.assertEqual(IssuedBook.objects.filter(book=book).count(), 1)
