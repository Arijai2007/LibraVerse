from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing

from apps.core.models import Library
from apps.core.views import staff_required
from apps.members.models import LibraryMembership
from apps.students.models import StudentExtra

from .catalogue import search_titles, visible_libraries
from .forms import TitleForm
from .models import Author, Book, BookCopy, BookHold, Category, IssuedBook
from .services import (
    add_copies,
    archive_title,
    audit,
    change_copy_status,
    issue_book,
    lock_title,
    return_book,
    sync_taxonomy,
)


class CopyIssueForm(forms.Form):
    borrower = forms.ModelChoiceField(queryset=User.objects.none(), required=False)
    reader = forms.ModelChoiceField(
        queryset=StudentExtra.objects.none(),
        required=False,
        label="Offline reader",
    )
    due = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}), label="Due date"
    )

    def __init__(self, library, hold, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due"].initial = timezone.localdate() + timedelta(
            days=max(1, settings.LIBRAVERSE_LOAN_DAYS)
        )
        borrowers = (
            User.objects.filter(is_active=True)
            .filter(
                Q(pk=library.owner_id)
                | Q(
                    memberships__library=library,
                    memberships__must_change_password=False,
                )
            )
            .distinct()
            .order_by("username")
        )
        self.fields["borrower"].queryset = borrowers
        if hold and hold.reservation.member_id and hold.expires_at > timezone.now():
            self.fields["borrower"].queryset = borrowers.filter(
                pk=hold.reservation.member_id
            )
            self.fields["borrower"].initial = hold.reservation.member_id
            del self.fields["reader"]
        else:
            self.fields["reader"].queryset = StudentExtra.objects.filter(
                library=library
            ).order_by("name")

    def clean(self):
        values = super().clean()
        if bool(values.get("borrower")) == bool(values.get("reader")):
            raise forms.ValidationError("Choose one borrower or offline reader.")
        return values


@staff_required
@require_GET
def inventory(request):
    library = request.library
    term = request.GET.get("q", "").strip()[:120]
    category = request.GET.get("category", "")[:50]
    archived = request.GET.get("archived") == "1"
    books = search_titles(Book.objects.filter(library=library, archived=archived), term)
    if category:
        books = books.filter(category=category)
    books = books.annotate(
        total_copies=Count("copies", filter=~Q(copies__status=BookCopy.Status.RETIRED))
    )
    return render(
        request,
        "verse/inventory.html",
        {
            "library": library,
            "page": Paginator(books.order_by("name", "pk"), 15).get_page(
                request.GET.get("page")
            ),
            "term": term,
            "category": category,
            "archived": archived,
            "categories": library.categories.all(),
        },
    )


@staff_required
def title_inventory(request, pk):
    library = request.library
    book = get_object_or_404(Book, pk=pk, library=library)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "edit":
                with transaction.atomic():
                    locked = lock_title(book.pk, library=library)
                    form = TitleForm(request.POST, request.FILES, instance=locked)
                    if not form.is_valid():
                        return render_title(request, book, form)
                    locked = form.save()
                    sync_taxonomy(locked)
                    audit(locked, "Title updated", request.user)
            elif action == "add":
                add_copies(
                    book.pk, library, int(request.POST.get("count", "")), request.user
                )
            elif action == "archive":
                if request.POST.get("confirm") != "yes":
                    raise ValueError("Confirm archiving before continuing.")
                archive_title(book.pk, library, request.user)
            elif action == "restore":
                with transaction.atomic():
                    locked = lock_title(book.pk, library=library)
                    locked.archived = False
                    locked.save(update_fields=["archived"])
                    audit(locked, "Title restored", request.user)
            else:
                raise ValueError("Choose a valid inventory action.")
        except (ValueError, Book.DoesNotExist) as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Inventory updated.")
        return redirect("title_inventory", pk=pk)
    return render_title(request, book, TitleForm(instance=book))


def render_title(request, book, form):
    copies = book.copies.order_by("pk")
    status = request.GET.get("status", "")
    if status in BookCopy.Status.values:
        copies = copies.filter(status=status)
    return render(
        request,
        "verse/title_inventory.html",
        {
            "library": request.library,
            "book": book,
            "form": form,
            "status": status,
            "statuses": BookCopy.Status.choices,
            "page": Paginator(copies, 25).get_page(request.GET.get("page")),
        },
    )


@staff_required
def taxonomy(request):
    library = request.library
    if request.method == "POST":
        kinds = {
            "author": (Author, "author_record", "author"),
            "category": (Category, "category_record", "category"),
        }
        kind = kinds.get(request.POST.get("kind"))
        if not kind:
            raise PermissionDenied
        model, relation, field = kind
        item_id = request.POST.get("item", "")
        name = request.POST.get("name", "").strip()
        if not name or len(name) > model._meta.get_field("name").max_length:
            messages.error(request, "Enter a valid name.")
            return redirect("taxonomy")
        try:
            with transaction.atomic():
                Library.objects.select_for_update(no_key=True).get(pk=library.pk)
                if item_id.isdecimal():
                    item = get_object_or_404(model, pk=item_id, library=library)
                    item.name = name
                    item.save(update_fields=["name"])
                    Book.objects.filter(library=library, **{relation: item}).update(
                        **{field: name}
                    )
                else:
                    model.objects.create(library=library, name=name)
        except IntegrityError:
            messages.error(request, "That name already exists in this library.")
        else:
            messages.success(request, "Catalogue reference updated.")
        return redirect("taxonomy")
    return render(
        request,
        "verse/taxonomy.html",
        {
            "library": library,
            "sections": [
                ("author", "Authors", library.authors.all()),
                ("category", "Categories", library.categories.all()),
            ],
        },
    )


@staff_required
@require_POST
def catalogue_visibility(request):
    library = request.library
    if request.user.pk != library.owner_id:
        raise PermissionDenied
    with transaction.atomic():
        library = Library.objects.select_for_update(no_key=True).get(pk=library.pk)
        library.public_catalogue = request.POST.get("public") == "yes"
        library.save(update_fields=["public_catalogue"])
        library.circulation_activity.create(
            actor=request.user,
            action="Public catalogue enabled"
            if library.public_catalogue
            else "Public catalogue disabled",
        )
    messages.success(request, "Catalogue visibility updated.")
    return redirect("dashboard")


@staff_required
@require_GET
def circulation_desk(request):
    library = request.library
    code = request.GET.get("code", "").strip().upper()[:19]
    if code:
        copy = BookCopy.objects.filter(book__library=library, code=code).first()
        if copy:
            return redirect("copy_detail", code=copy.code)
        messages.error(request, "No copy with that ID exists in this library.")
    incoming = (
        BookHold.objects.filter(
            copy__book__library=library,
            ended_at__isnull=True,
            expires_at__gt=timezone.now(),
            reservation__member__isnull=False,
        )
        .select_related("copy__book", "reservation__member")
        .order_by("expires_at")[:20]
    )
    return render(
        request,
        "verse/desk.html",
        {"library": library, "incoming": incoming, "code": code},
    )


def _staff_can_view_copy(user, copy):
    library = copy.book.library
    if not user.is_authenticated:
        return False
    return (
        user.pk == library.owner_id
        or LibraryMembership.objects.filter(
            library=library,
            user=user,
            role="librarian",
            must_change_password=False,
        ).exists()
    )


@never_cache
@require_GET
def copy_detail(request, code):
    copy = (
        BookCopy.objects.select_related("book__library")
        .filter(code=code.upper())
        .first()
    )
    if not request.user.is_authenticated and (
        not copy
        or not copy.book.library.public_catalogue
        or copy.book.archived
        or copy.status == BookCopy.Status.RETIRED
    ):
        return redirect_to_login(request.path)
    if not copy:
        raise Http404
    library = copy.book.library
    staff = _staff_can_view_copy(request.user, copy)
    if not staff:
        get_object_or_404(visible_libraries(request.user), pk=library.pk)
        if copy.book.archived or copy.status == BookCopy.Status.RETIRED:
            raise Http404
        return render(request, "verse/copy_public.html", {"copy": copy})
    if request.session.get("current_library_id") != library.pk:
        return render(
            request, "verse/copy_public.html", {"copy": copy, "open_staff": True}
        )
    request.library = library
    request.can_circulate = True
    hold = (
        copy.holds.filter(ended_at__isnull=True, reservation__member__isnull=False)
        .select_related("reservation__member")
        .first()
    )
    history = copy.loans.select_related("student", "borrower").order_by(
        "-issuedate", "-pk"
    )
    return render(
        request,
        "verse/copy_staff.html",
        {
            "library": library,
            "copy": copy,
            "hold": hold,
            "loan": history.filter(returned=False).first(),
            "history": Paginator(history, 10).get_page(request.GET.get("page")),
            "now": timezone.now(),
            "issue_form": CopyIssueForm(library, hold),
            "qr_url": _copy_url(request, copy),
        },
    )


def _copy_url(request, copy):
    path = reverse("copy_detail", args=[copy.code])
    return (
        settings.PUBLIC_BASE_URL + path
        if settings.PUBLIC_BASE_URL
        else request.build_absolute_uri(path)
    )


@staff_required
@require_POST
def copy_action(request, code):
    copy = get_object_or_404(
        BookCopy.objects.select_related("book"),
        code=code,
        book__library=request.library,
    )
    try:
        action = request.POST.get("action")
        if action == "issue":
            hold = (
                copy.holds.filter(ended_at__isnull=True)
                .select_related("reservation")
                .first()
            )
            form = CopyIssueForm(request.library, hold, request.POST)
            if not form.is_valid():
                raise ValueError(
                    " ".join(
                        message for errors in form.errors.values() for message in errors
                    )
                )
            issue_book(
                form.cleaned_data.get("reader"),
                copy.book,
                form.cleaned_data["due"],
                copy_id=copy.pk,
                borrower=form.cleaned_data["borrower"],
                actor=request.user,
            )
        elif action == "return":
            loan = copy.loans.get(pk=int(request.POST.get("loan", "")))
            return_book(loan.pk, request.library, request.user)
        elif action == "status":
            if (
                request.POST.get("status") == BookCopy.Status.RETIRED
                and request.POST.get("confirm") != "yes"
            ):
                raise ValueError("Confirm before retiring a copy.")
            change_copy_status(copy, request.POST.get("status"), request.user)
        else:
            raise ValueError("Choose a valid circulation action.")
    except (ValueError, IssuedBook.DoesNotExist, Book.DoesNotExist) as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Copy updated.")
    return redirect("copy_detail", code=copy.code)


@staff_required
@require_GET
def copy_qr(request, code):
    copy = get_object_or_404(BookCopy, code=code, book__library=request.library)
    qr = QrCodeWidget(_copy_url(request, copy), barWidth=180, barHeight=180)
    drawing = Drawing(180, 180)
    drawing.add(qr)
    return HttpResponse(renderSVG.drawToString(drawing), content_type="image/svg+xml")
