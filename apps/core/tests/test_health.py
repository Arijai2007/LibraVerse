from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse


class DeploymentHealthTests(TestCase):
    def test_health_returns_ok_when_database_is_reachable(self):
        response = self.client.get(reverse("health_check"))
        self.assertContains(response, "ok")
        self.assertIn("no-store", response["Cache-Control"])

    def test_health_returns_unavailable_on_database_error(self):
        with patch("apps.core.views.connection.cursor", side_effect=DatabaseError):
            response = self.client.get(reverse("health_check"))
        self.assertContains(response, "unavailable", status_code=503)
