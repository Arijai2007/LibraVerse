"""Transactional inventory and circulation. Lock a title before changing its copies."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Library
from apps.members.models import LibraryMembership

from .models import (
    ActivityLog,
    Author,
    Book,
    BookCopy,
    BookHold,
    Category,
    IssuedBook,
    Notification,
    Reservation,
)

OPEN = (Reservation.Status.WAITING, Reservation.Status.HELD)


def audit(book, action, actor=None):
    ActivityLog.objects.create(
        library_id=book.library_id, book=book, actor=actor, action=action
    )


def notify(book, user_id, message, key=None):
    if user_id:
        values = dict(
            library_id=book.library_id, book=book, recipient_id=user_id, message=message
        )
        if key:
            Notification.objects.get_or_create(event_key=key, defaults=values)
        else:
            Notification.objects.create(**values)


def lock_title(book_id, **filters):
    library_id = Book.objects.values_list("library_id", flat=True).get(
        pk=book_id, **filters
    )
    # Serialize circulation across titles within one library so eligibility
    # and member-removal changes observe the same committed loans.
    Library.objects.select_for_update(no_key=True).get(pk=library_id)
    return Book.objects.select_for_update().get(pk=book_id, **filters)


def _available(book):
    return book.copies.filter(status=BookCopy.Status.AVAILABLE).order_by("pk")


def _refresh_stock(book):
    Book.objects.filter(pk=book.pk).update(quantity=_available(book).count())


def _new_copies(book, count):
    for start in range(0, count, 500):
        BookCopy.objects.bulk_create(
            [BookCopy(book=book) for _ in range(min(500, count - start))]
        )


def _bootstrap_legacy_stock(book):
    # Also supports legacy integrations that instantiate a title with a stock count.
    if book.quantity and not book.copies.exists():
        _new_copies(book, book.quantity)


def sync_taxonomy(book):
    book.author_record = (
        Author.objects.get_or_create(library_id=book.library_id, name=book.author)[0]
        if book.author
        else None
    )
    book.category_record = (
        Category.objects.get_or_create(library_id=book.library_id, name=book.category)[
            0
        ]
        if book.category
        else None
    )
    book.save(update_fields=["author_record", "category_record"])


@transaction.atomic
def initialize_title(book, actor=None):
    book = lock_title(book.pk)
    sync_taxonomy(book)
    _bootstrap_legacy_stock(book)
    audit(book, "Title added", actor)


def eligibility(book, user):
    if user is None:
        return ""
    if not user.is_active:
        return "This account is inactive."
    membership = (
        LibraryMembership.objects.select_for_update()
        .filter(
            library_id=book.library_id,
            user=user,
        )
        .first()
    )
    if user.pk != book.library.owner_id and (
        not membership or membership.must_change_password
    ):
        return "An active library membership is required."
    active = IssuedBook.objects.filter(
        returned=False, book__library_id=book.library_id
    ).filter(
        Q(borrower=user) | Q(student__user=user, student__library_id=book.library_id)
    )
    if active.filter(book=book).exists():
        return "You already have an open loan for this title."
    if active.filter(return_date__lt=timezone.localdate()).exists():
        return "Return overdue loans before borrowing or reserving another title."
    return ""


def _close_hold(hold, status, now):
    BookHold.objects.filter(pk=hold.pk).update(ended_at=now)
    Reservation.objects.filter(pk=hold.reservation_id).update(status=status)
    BookCopy.objects.filter(pk=hold.copy_id).update(status=BookCopy.Status.AVAILABLE)


def _reconcile(book, now):
    closed = list(
        BookHold.objects.filter(copy__book=book, ended_at__isnull=True)
        .filter(Q(expires_at__lte=now) | Q(reservation__member__isnull=True))
        .select_related("reservation")
    )
    expired_count = 0
    for hold in closed:
        if hold.reservation.member_id is None:
            _close_hold(hold, Reservation.Status.CANCELLED, now)
            audit(book, "Unassigned pickup hold released")
        else:
            _close_hold(hold, Reservation.Status.EXPIRED, now)
            notify(
                book,
                hold.reservation.member_id,
                f"Your pickup hold for {book.name} expired.",
            )
            audit(book, "Pickup hold expired")
            expired_count += 1

    if not book.archived:
        entries = list(
            book.reservations.filter(
                status=Reservation.Status.WAITING, member__isnull=False
            )
            .select_related("member")
            .order_by("created_at", "pk")
        )
        waiting = iter(entries)
        for copy in _available(book)[: len(entries)]:
            for reservation in waiting:
                reason = eligibility(book, reservation.member)
                if reason:
                    Reservation.objects.filter(pk=reservation.pk).update(
                        status=Reservation.Status.CANCELLED
                    )
                    notify(
                        book,
                        reservation.member_id,
                        f"Reservation cancelled for {book.name}. {reason}",
                    )
                    audit(book, "Ineligible reservation cancelled")
                    continue
                BookHold.objects.create(
                    reservation=reservation,
                    copy=copy,
                    expires_at=now
                    + timedelta(hours=max(1, settings.LIBRAVERSE_HOLD_HOURS)),
                )
                Reservation.objects.filter(pk=reservation.pk).update(
                    status=Reservation.Status.HELD
                )
                BookCopy.objects.filter(pk=copy.pk).update(status=BookCopy.Status.HELD)
                notify(book, reservation.member_id, f"{book.name} is ready for pickup.")
                audit(book, "Pickup hold allocated")
                break
            else:
                break
    _refresh_stock(book)
    return expired_count


@transaction.atomic
def reconcile_title(book_id, now=None):
    book = lock_title(book_id)
    return _reconcile(book, now or timezone.now())


@transaction.atomic
def reserve(book_id, member):
    if member is None:
        raise ValueError("Sign in to reserve a title.")
    book = lock_title(book_id, archived=False)
    reason = eligibility(book, member)
    if reason:
        raise ValueError(reason)
    if book.reservations.filter(member=member, status__in=OPEN).exists():
        raise ValueError("You already have an active reservation for this title.")
    _bootstrap_legacy_stock(book)
    reservation = book.reservations.create(member=member)
    notify(book, member.pk, f"Reservation placed for {book.name}.")
    audit(book, "Reservation placed", member)
    _reconcile(book, timezone.now())
    reservation.refresh_from_db()
    return reservation


@transaction.atomic
def cancel_reservation(book_id, member):
    book = lock_title(book_id)
    reservation = book.reservations.filter(member=member, status__in=OPEN).first()
    if reservation is None:
        raise ValueError("There is no active reservation to cancel.")
    now = timezone.now()
    hold = BookHold.objects.filter(
        reservation=reservation, ended_at__isnull=True
    ).first()
    if hold and hold.expires_at <= now:
        _reconcile(book, now)
        return False
    if hold:
        _close_hold(hold, Reservation.Status.CANCELLED, now)
    else:
        Reservation.objects.filter(pk=reservation.pk).update(
            status=Reservation.Status.CANCELLED
        )
    notify(book, member.pk, f"Reservation cancelled for {book.name}.")
    audit(book, "Reservation cancelled", member)
    _reconcile(book, now)
    return True


def queue_position(reservation):
    if reservation and reservation.status == Reservation.Status.WAITING:
        return (
            1
            + reservation.book.reservations.filter(
                status=Reservation.Status.WAITING, member__isnull=False
            )
            .filter(
                Q(created_at__lt=reservation.created_at)
                | Q(created_at=reservation.created_at, pk__lt=reservation.pk)
            )
            .count()
        )
    return None


@transaction.atomic
def add_copies(book_id, library, count, actor):
    if not 1 <= count <= 500:
        raise ValueError("Enter between 1 and 500 copies.")
    book = lock_title(book_id, library=library, archived=False)
    _bootstrap_legacy_stock(book)
    _new_copies(book, count)
    audit(book, f"{count} copies added", actor)
    _reconcile(book, timezone.now())


@transaction.atomic
def change_copy_status(copy, status, actor):
    book = lock_title(copy.book_id, archived=False)
    copy.refresh_from_db()
    editable = {
        BookCopy.Status.AVAILABLE,
        BookCopy.Status.MAINTENANCE,
        BookCopy.Status.RETIRED,
    }
    if copy.status not in editable or status not in editable:
        raise ValueError(
            "Only available, maintenance or retired copies can change inventory status."
        )
    BookCopy.objects.filter(pk=copy.pk).update(status=status)
    audit(book, f"{copy.code} moved to {BookCopy.Status(status).label.lower()}", actor)
    _reconcile(book, timezone.now())


@transaction.atomic
def archive_title(book_id, library, actor):
    book = lock_title(book_id, library=library)
    if (
        IssuedBook.objects.filter(book=book, returned=False).exists()
        or book.reservations.filter(status__in=OPEN).exists()
    ):
        raise ValueError(
            "Close open loans and reservations before archiving this title."
        )
    Book.objects.filter(pk=book.pk).update(archived=True)
    audit(book, "Title archived", actor)


@transaction.atomic
def issue_book(student, book, return_date, *, copy_id=None, borrower=None, actor=None):
    from apps.students.models import StudentExtra

    book = lock_title(book.pk)
    if student is not None:
        student = StudentExtra.objects.filter(
            pk=student.pk, library_id=book.library_id
        ).first()
        if student is None:
            raise ValueError("Choose a reader from this library.")
    if book.archived:
        raise ValueError("Choose an active title and a reader from this library.")
    if not student and not borrower:
        raise ValueError("Choose a borrower.")
    if student and student.user and borrower and student.user_id != borrower.pk:
        raise ValueError("Choose a single borrower.")
    borrower = borrower or (student.user if student else None)
    has_student_pass = student is not None or (
        borrower is not None
        and StudentExtra.objects.filter(library_id=book.library_id, user=borrower).exists()
    )
    reason = eligibility(book, borrower)
    if reason:
        raise ValueError(reason)
    if (
        student
        and IssuedBook.objects.filter(
            book=book, student=student, returned=False
        ).exists()
    ):
        raise ValueError("This reader already has an open loan for this title.")
    if (
        student
        and IssuedBook.objects.filter(
            book__library_id=book.library_id,
            student=student,
            returned=False,
            return_date__lt=timezone.localdate(),
        ).exists()
    ):
        raise ValueError("This reader has an overdue loan.")
    if return_date < timezone.localdate():
        raise ValueError("Choose a due date of today or later.")

    _bootstrap_legacy_stock(book)
    now = timezone.now()
    _reconcile(book, now)
    hold = None
    if borrower:
        hold = (
            BookHold.objects.filter(
                reservation__book=book,
                reservation__member=borrower,
                ended_at__isnull=True,
            )
            .select_related("reservation", "copy")
            .first()
        )
    if hold and (copy_id is None or copy_id == hold.copy_id):
        copy = hold.copy
        _close_hold(hold, Reservation.Status.FULFILLED, now)
    else:
        copy = (
            _available(book).filter(pk=copy_id).first()
            if copy_id
            else _available(book).first()
        )
        if not copy:
            raise ValueError(
                "This copy is out of stock or reserved for another reader."
            )
        if hold:
            raise ValueError("Use the copy assigned to this reader’s pickup hold.")
    loan = IssuedBook.objects.create(
        student=student,
        book=book,
        copy=copy,
        borrower=borrower,
        enrollment=student.enrollment if student else f"LV-{borrower.pk}",
        book_name=book.name,
        return_date=return_date,
        fine_daily_rate=book.library.fine_daily_rate if has_student_pass else 0,
        fine_grace_days=book.library.fine_grace_days if has_student_pass else 0,
    )
    BookCopy.objects.filter(pk=copy.pk).update(status=BookCopy.Status.BORROWED)
    _refresh_stock(book)
    notify(
        book,
        loan.borrower_id,
        f"{book.name} was issued to you. Due {return_date:%d %b %Y}.",
    )
    audit(book, "Copy issued", actor)
    return loan


@transaction.atomic
def return_book(issued_book_id, library, actor=None):
    book_id = IssuedBook.objects.values_list("book_id", flat=True).get(
        pk=issued_book_id,
        book__library=library,
    )
    book = lock_title(book_id, library=library)
    loan = IssuedBook.objects.get(pk=issued_book_id, book=book)
    if loan.returned:
        return loan
    _bootstrap_legacy_stock(book)
    if not loan.copy_id:
        loan.copy = BookCopy.objects.create(book=book, status=BookCopy.Status.BORROWED)
    now = timezone.now()
    loan.returned = True
    loan.returned_at = now
    if loan.return_date < timezone.localdate(now):
        loan.overdue_on = loan.return_date + timedelta(days=1)
    loan.save(update_fields=["copy", "returned", "returned_at", "overdue_on"])
    BookCopy.objects.filter(pk=loan.copy_id).update(status=BookCopy.Status.AVAILABLE)
    notify(book, loan.borrower_id, f"{book.name} was returned.")
    audit(book, "Copy returned", actor)
    _reconcile(book, now)
    return loan


@transaction.atomic
def settle_fine(loan_id, student, status, actor):
    if status not in (IssuedBook.FineStatus.PAID, IssuedBook.FineStatus.WAIVED):
        raise ValueError("Choose a valid fine action.")
    # Match the lock order used by checkout and return.
    Library.objects.select_for_update(no_key=True).get(pk=student.library_id)
    loan = student_loans(student).select_for_update(of=("self",)).get(pk=loan_id)
    amount = loan.overdue_fine()
    if not loan.returned or amount <= 0:
        raise ValueError("Only returned loans with a fine can be settled.")
    if loan.fine_status != IssuedBook.FineStatus.OUTSTANDING:
        raise ValueError("This fine has already been settled.")
    loan.fine_status = status
    loan.save(update_fields=["fine_status"])
    audit(loan.book, f"Loan #{loan.pk} fine {status} (INR {amount:.2f})", actor)


def student_loans(student):
    identity = Q(student=student)
    if student.user_id:
        identity |= Q(borrower_id=student.user_id, student__isnull=True)
    return IssuedBook.objects.filter(identity, book__library_id=student.library_id)


def set_available_stock(book, count, actor):
    """Preserve the legacy bulk-edit operation using actual copies."""
    if not 0 <= count <= 500 or book.archived:
        raise ValueError(
            "Available stock must be between 0 and 500 for an active title."
        )
    existing = _available(book).count()
    if count > existing:
        _new_copies(book, count - existing)
    elif count < existing:
        retiring = list(
            _available(book).reverse().values_list("pk", flat=True)[: existing - count]
        )
        book.copies.filter(pk__in=retiring).update(status=BookCopy.Status.RETIRED)
    if count != existing:
        audit(book, "Available stock adjusted", actor)
    _reconcile(book, timezone.now())


@transaction.atomic
def send_due_notice(loan_id, today):
    loan = IssuedBook.objects.select_related("book").get(pk=loan_id)
    book = lock_title(loan.book_id)
    loan.refresh_from_db()
    if loan.returned:
        return
    due = loan.return_date
    if due < today:
        if not loan.overdue_on:
            IssuedBook.objects.filter(pk=loan.pk).update(
                overdue_on=due + timedelta(days=1)
            )
        notify(
            book,
            loan.borrower_id,
            f"{book.name} is overdue. It was due {due:%d %b %Y}.",
            f"loan:{loan.pk}:overdue:{due}",
        )
    elif due <= today + timedelta(days=max(0, settings.LIBRAVERSE_REMINDER_DAYS)):
        notify(
            book,
            loan.borrower_id,
            f"{book.name} is due {due:%d %b %Y}.",
            f"loan:{loan.pk}:due:{due}",
        )
