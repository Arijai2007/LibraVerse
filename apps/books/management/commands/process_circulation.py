from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.books.models import Book, BookHold, IssuedBook, Reservation
from apps.books.services import reconcile_title, send_due_notice


class Command(BaseCommand):
    help = (
        "Expire pickup holds, allocate free copies and generate in-app loan reminders."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        today = timezone.localdate(now)
        Reservation.objects.filter(
            status=Reservation.Status.WAITING, member__isnull=True
        ).update(
            status=Reservation.Status.CANCELLED,
        )
        expired_titles = (
            BookHold.objects.filter(
                ended_at__isnull=True,
            )
            .filter(Q(expires_at__lte=now) | Q(reservation__member__isnull=True))
            .values_list("copy__book_id", flat=True)
        )
        waiting_titles = Reservation.objects.filter(
            status=Reservation.Status.WAITING,
            book__quantity__gt=0,
        ).values_list("book_id", flat=True)
        titles = (
            Book.objects.filter(pk__in=expired_titles)
            .union(Book.objects.filter(pk__in=waiting_titles))
            .values_list("pk", flat=True)
        )
        expired = sum(reconcile_title(pk, now) for pk in list(titles))
        due = IssuedBook.objects.filter(
            returned=False,
            book__isnull=False,
            return_date__lte=today
            + timedelta(days=max(0, settings.LIBRAVERSE_REMINDER_DAYS)),
        ).exclude(borrower__isnull=True, overdue_on__isnull=False)
        for pk in due.values_list("pk", flat=True).iterator():
            send_due_notice(pk, today)
        self.stdout.write(
            self.style.SUCCESS(f"Circulation processed. Expired holds: {expired}.")
        )
