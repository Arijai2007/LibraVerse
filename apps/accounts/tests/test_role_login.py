from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.core.models import Library
from apps.members.models import LibraryMembership


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RoleLoginTests(TestCase):
    password = "Example-login-431!"

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner-login", password=cls.password)
        cls.librarian = User.objects.create_user("librarian-login", password=cls.password)
        cls.reader = User.objects.create_user("reader-login", password=cls.password)
        cls.library = Library.objects.create(
            name="Role Login Library", owner=cls.owner, public_catalogue=True
        )
        for user, role in [(cls.librarian, "librarian"), (cls.reader, "member")]:
            LibraryMembership.objects.create(
                library=cls.library, user=user, role=role, must_change_password=False
            )

    def credentials(self, user):
        return {"username": user.username, "password": self.password}

    def test_staff_accounts_use_staff_portal(self):
        for user in (self.owner, self.librarian):
            with self.subTest(user=user.username):
                client = Client()
                response = client.post(reverse("adminlogin"), self.credentials(user))
                self.assertContains(response, "Use Staff sign in")
                self.assertNotIn(SESSION_KEY, client.session)
                self.assertRedirects(
                    client.post(reverse("staff_login"), self.credentials(user)),
                    reverse("dashboard"),
                )
                self.assertEqual(client.get(reverse("inventory")).status_code, 200)
                self.assertEqual(
                    client.get(reverse("view_members")).status_code,
                    200 if user == self.owner else 403,
                )

    def test_reader_uses_public_portal_and_cannot_access_inventory(self):
        response = self.client.post(reverse("staff_login"), self.credentials(self.reader))
        self.assertContains(response, "Use Reader sign in")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertRedirects(
            self.client.post(reverse("adminlogin"), self.credentials(self.reader)),
            reverse("dashboard"),
        )
        self.assertContains(self.client.get(reverse("dashboard")), "Current loans")
        self.assertEqual(self.client.get(reverse("inventory")).status_code, 403)

    def test_public_registration_ignores_a_submitted_staff_role(self):
        self.assertRedirects(
            self.client.post(
                reverse("register_member"),
                {
                    "username": "new-role-reader",
                    "password1": self.password,
                    "password2": self.password,
                    "library": self.library.code,
                    "role": "owner",
                },
            ),
            reverse("dashboard"),
        )
        membership = self.library.memberships.get(user__username="new-role-reader")
        self.assertEqual(membership.role, "member")

    def test_staff_library_picker_cannot_finish_at_reader_portal(self):
        other = Library.objects.create(name="Second Staff Library", owner=self.owner)
        response = self.client.post(reverse("staff_login"), self.credentials(self.owner))
        self.assertContains(response, "Choose your library")
        response = self.client.post(reverse("adminlogin"), {"selected_library": other.pk})
        self.assertContains(response, "Please sign in again")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_allauth_login_redirects_staff_to_staff_portal(self):
        response = self.client.post(
            reverse("account_login"),
            {"login": self.owner.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("staff_login"))
        self.assertNotIn(SESSION_KEY, self.client.session)
