from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class PublicLogoutTests(TestCase):
    def test_visitors_do_not_see_logout(self):
        self.assertNotContains(self.client.get(reverse("home")), "Log out")

    def test_reader_can_log_out_from_public_navigation(self):
        reader = get_user_model().objects.create_user(username="logout-reader")
        client = Client(enforce_csrf_checks=True)
        client.force_login(reader)
        logout = reverse("logout")
        for route in ("home", "catalogue", "book_match"):
            with self.subTest(route=route):
                page = client.get(reverse(route))
                self.assertContains(page, f'method="post" action="{logout}"')
                self.assertContains(page, "Log out")

        self.assertEqual(client.get(logout).status_code, 405)
        self.assertEqual(client.post(logout).status_code, 403)
        self.assertIn(SESSION_KEY, client.session)
        self.assertRedirects(
            client.post(logout, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}),
            reverse("home"),
        )
        self.assertNotIn(SESSION_KEY, client.session)
        self.assertNotContains(client.get(reverse("home")), "Log out")
