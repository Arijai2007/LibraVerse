from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import User
import uuid


class Library(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=12, unique=True, blank=True)
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="owned_libraries"
    )
    public_catalogue = models.BooleanField(default=False)
    fine_daily_rate = models.DecimalField(
        max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    fine_grace_days = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(365)]
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = "LIB-" + uuid.uuid4().hex[:6].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.code}]"

    class Meta:
        app_label = "library"


class AdminProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="admin_profile"
    )
    library = models.ForeignKey(
        "Library", on_delete=models.SET_NULL, null=True, blank=True
    )
    phone = models.CharField(max_length=20, blank=True, default="")
    address = models.CharField(max_length=100, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    photo = models.ImageField(upload_to="admin_photos/", null=True, blank=True)

    def __str__(self):
        return f"Profile: {self.user.username}"

    class Meta:
        app_label = "library"
