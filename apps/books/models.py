from datetime import datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Library
from apps.students.models import StudentExtra


class Book(models.Model):
    CATEGORY_CHOICES = [
        ("Education", "Education"),
        ("History", "History"),
        ("Novel", "Novel"),
        ("Fiction", "Fiction"),
        ("Thriller/Mystery", "Thriller/Mystery"),
        ("Romance", "Romance"),
        ("Scifi", "Sci-Fi"),
        ("Poetry", "Poetry"),
        ("Adventure", "Adventure"),
        ("Religion", "Religion & Spirituality"),
    ]

    LANGUAGE_CHOICES = [
        ("English", "English"),
        ("Urdu", "Urdu"),
    ]

    library = models.ForeignKey(Library, on_delete=models.CASCADE, related_name="books")
    name = models.CharField(max_length=100, db_index=True)
    quantity = models.PositiveIntegerField()
    author = models.CharField(max_length=40)
    category = models.CharField(max_length=50, default="Education")
    language = models.CharField(max_length=30, default="English")
    isbn = models.CharField(max_length=13, blank=True, db_index=True)
    description = models.TextField(blank=True)
    cover = models.ImageField(upload_to="book_covers/", blank=True)
    archived = models.BooleanField(default=False)
    author_record = models.ForeignKey(
        "Author", null=True, blank=True, on_delete=models.SET_NULL, related_name="books"
    )
    category_record = models.ForeignKey(
        "Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )

    class Meta:
        app_label = "library"
        indexes = [models.Index(fields=["library", "archived", "name"])]

    def __str__(self):
        return f"{self.name} [{self.quantity}]"


def get_expiry():
    return datetime.today() + timedelta(days=15)


class IssuedBook(models.Model):
    class FineStatus(models.TextChoices):
        OUTSTANDING = "outstanding", "Outstanding"
        PAID = "paid", "Paid"
        WAIVED = "waived", "Waived"

    # ForeignKey for better relationships
    student = models.ForeignKey(
        StudentExtra, on_delete=models.SET_NULL, null=True, blank=True
    )
    book = models.ForeignKey(Book, on_delete=models.CASCADE, null=True, blank=True)

    enrollment = models.CharField(max_length=30, db_index=True)
    book_name = models.CharField(max_length=200, blank=True)

    copy = models.ForeignKey(
        "BookCopy",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="loans",
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="library_loans",
    )
    issuedate = models.DateField(default=timezone.localdate)
    expirydate = models.DateField(default=get_expiry)
    return_date = models.DateField(default=get_expiry)
    returned = models.BooleanField(default=False, db_index=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    legacy = models.BooleanField(default=False, editable=False)
    overdue_on = models.DateField(null=True, blank=True)
    fine_daily_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    fine_grace_days = models.PositiveSmallIntegerField(default=0)
    fine_status = models.CharField(
        max_length=11, choices=FineStatus, default=FineStatus.OUTSTANDING
    )

    def overdue_days(self, today=None):
        if self.returned:
            if not self.returned_at:
                return None
            today = timezone.localdate(self.returned_at)
        return max(0, ((today or timezone.localdate()) - self.return_date).days)

    def overdue_fine(self, today=None):
        chargeable_days = max(0, (self.overdue_days(today) or 0) - self.fine_grace_days)
        return self.fine_daily_rate * chargeable_days

    class Meta:
        app_label = "library"
        constraints = [
            models.UniqueConstraint(
                fields=["copy"],
                condition=Q(returned=False, copy__isnull=False),
                name="one_open_loan_per_copy",
            ),
            models.UniqueConstraint(
                fields=["book", "borrower"],
                condition=Q(returned=False, borrower__isnull=False, legacy=False),
                name="one_open_loan_per_member_title",
            ),
            models.UniqueConstraint(
                fields=["book", "student"],
                condition=Q(
                    returned=False,
                    student__isnull=False,
                    copy__isnull=False,
                    borrower__isnull=True,
                    legacy=False,
                ),
                name="one_open_loan_per_unlinked_reader",
            ),
        ]
        indexes = [models.Index(fields=["book", "returned", "return_date"])]

    def __str__(self):
        return f"{self.enrollment} - {self.book_name}"

    def save(self, *args, **kwargs):
        # Auto-fill enrollment and book_name if foreign keys are provided
        if self.student and not self.enrollment:
            self.enrollment = self.student.enrollment
        if self.book and not self.book_name:
            self.book_name = self.book.name
        super().save(*args, **kwargs)


class Author(models.Model):
    library = models.ForeignKey(
        Library, on_delete=models.CASCADE, related_name="authors"
    )
    name = models.CharField(max_length=40)

    class Meta:
        app_label = "library"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["library", "name"], name="unique_library_author"
            )
        ]

    def __str__(self):
        return self.name


class Category(models.Model):
    library = models.ForeignKey(
        Library, on_delete=models.CASCADE, related_name="categories"
    )
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "library"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["library", "name"], name="unique_library_category"
            )
        ]

    def __str__(self):
        return self.name


def copy_code():
    return "LV-" + uuid4().hex[:16].upper()


class BookCopy(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        BORROWED = "borrowed", "Borrowed"
        HELD = "held", "On hold"
        MAINTENANCE = "maintenance", "Maintenance"
        RETIRED = "retired", "Retired"

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="copies")
    code = models.CharField(
        max_length=19, default=copy_code, unique=True, editable=False
    )
    status = models.CharField(max_length=12, choices=Status, default=Status.AVAILABLE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "library"
        indexes = [models.Index(fields=["book", "status"])]

    def __str__(self):
        return self.code


class Reservation(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        HELD = "held", "Ready for pickup"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        FULFILLED = "fulfilled", "Collected"

    book = models.ForeignKey(
        Book, on_delete=models.CASCADE, related_name="reservations"
    )
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reservations",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=12, choices=Status, default=Status.WAITING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "library"
        indexes = [models.Index(fields=["book", "status", "created_at", "id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["book", "member"],
                condition=Q(status__in=["waiting", "held"]),
                name="one_active_reservation_per_title",
            ),
        ]


class BookHold(models.Model):
    reservation = models.OneToOneField(
        Reservation, on_delete=models.CASCADE, related_name="hold"
    )
    copy = models.ForeignKey(BookCopy, on_delete=models.RESTRICT, related_name="holds")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "library"
        constraints = [
            models.UniqueConstraint(
                fields=["copy"],
                condition=Q(ended_at__isnull=True),
                name="one_active_hold_per_copy",
            ),
        ]


class Notification(models.Model):
    library = models.ForeignKey(Library, on_delete=models.CASCADE)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="library_notifications",
    )
    book = models.ForeignKey(Book, on_delete=models.SET_NULL, null=True, blank=True)
    message = models.CharField(max_length=300)
    event_key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "library"
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["recipient", "library", "read_at"])]


class ActivityLog(models.Model):
    library = models.ForeignKey(
        Library, on_delete=models.CASCADE, related_name="circulation_activity"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    book = models.ForeignKey(Book, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "library"
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["library", "-created_at"])]
