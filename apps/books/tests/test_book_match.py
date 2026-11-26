from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.books.models import Book, Reservation
from apps.core.models import Library
from apps.members.models import LibraryMembership


class BookMatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        owner = User.objects.create_user("match-owner")
        cls.reader = User.objects.create_user("match-reader")
        cls.library = Library.objects.create(
            name="Public Library", owner=owner, public_catalogue=True
        )
        cls.private = Library.objects.create(name="Private Library", owner=owner)
        LibraryMembership.objects.create(
            user=cls.reader, library=cls.library, must_change_password=False
        )
        cls.title = Book.objects.create(
            name="The Missing Key",
            author="A. Writer",
            library=cls.library,
            category="Mystery",
            description="A detective novella.",
            quantity=1,
        )

    def test_recommends_visible_titles_with_reasons_and_availability(self):
        for name, library, archived, quantity in [
            ("Unavailable Mystery", self.library, False, 0),
            ("Another Mystery", self.library, False, 1),
            ("Fourth Mystery", self.library, False, 1),
            ("Private Mystery", self.private, False, 1),
            ("Archived Mystery", self.library, True, 1),
        ]:
            Book.objects.create(
                name=name,
                author="A. Writer",
                library=library,
                category="Mystery",
                quantity=quantity,
                archived=archived,
            )
        url = reverse("book_match")
        response = self.client.get(url, {"q": "I want a short mystery this weekend"})
        self.assertEqual(len(response.context["matches"]), 3)
        self.assertEqual(response.context["matches"][0]["book"], self.title)
        self.assertContains(response, "novella, mystery")
        self.assertContains(response, "1 available")
        for name in ("Private Mystery", "Archived Mystery", "Unavailable Mystery"):
            self.assertNotContains(response, name)
        private = self.client.get(url, {"q": "mystery", "library": self.private.code})
        self.assertContains(private, "No matching titles found.")

    def test_available_only_filters_out_waitlisted_titles(self):
        self.title.quantity = 0
        self.title.save(update_fields=["quantity"])
        params = {"q": "mystery", "library": self.library.code}
        url = reverse("book_match")
        self.assertContains(self.client.get(url, params), "Waitlist")
        self.assertContains(
            self.client.get(url, {**params, "available": "1"}), "No matching titles found."
        )

    def test_member_can_reserve_from_a_recommendation(self):
        self.client.force_login(self.reader)
        url = reverse("book_match")
        action = reverse("reservation_action", args=[self.title.pk])
        detail = reverse("book_detail", args=[self.title.pk])
        response = self.client.get(url, {"q": "detective"})
        self.assertContains(response, f'action="{action}"')
        self.assertContains(response, "Reserve for pickup")
        self.assertRedirects(self.client.post(action, {"action": "reserve"}), detail)
        self.assertTrue(
            Reservation.objects.filter(
                book=self.title, member=self.reader, status=Reservation.Status.HELD
            ).exists()
        )
        response = self.client.get(url, {"q": "detective"})
        self.assertContains(response, "View reservation")
        self.assertNotContains(response, f'action="{action}"')
