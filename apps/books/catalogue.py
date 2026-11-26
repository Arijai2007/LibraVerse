from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.core.models import Library
from apps.core.views import library_required
from apps.members.models import LibraryMembership

from .book_match import recommendations
from .models import Book, BookCopy, IssuedBook, Notification, Reservation
from .services import OPEN, cancel_reservation, queue_position, reserve

SORTS = {
    "title": ("name", "pk"),
    "author": ("author", "name", "pk"),
    "available": ("-quantity", "name", "pk"),
    "recent": ("-pk",),
}

MIN_SUGGESTION_LENGTH = 3


def visible_libraries(user):
    libraries = Library.objects.all()
    visibility = Q(public_catalogue=True)
    if user.is_authenticated:
        visibility |= Q(owner=user) | Q(
            memberships__user=user, memberships__must_change_password=False
        )
    return libraries.filter(visibility).distinct()


def visible_books(user):
    return Book.objects.filter(
        library__in=visible_libraries(user), archived=False
    ).select_related("library")


def search_titles(books, term):
    if term:
        isbn = term.replace("-", "").replace(" ", "")
        criteria = Q(name__icontains=term) | Q(author__icontains=term)
        if isbn:
            criteria |= Q(isbn__icontains=isbn)
        books = books.filter(criteria)
    return books


def title_suggestions(books, detail_view, include_library=False):
    return JsonResponse(
        {
            "results": [
                {
                    "title": book.name,
                    "author": book.author,
                    "library": book.library.name if include_library else "",
                    "url": reverse(detail_view, args=[book.pk]),
                }
                for book in books.order_by("name", "pk")[:7]
            ]
        }
    )


def _catalogue_filters(params):
    return {
        "term": params.get("q", "").strip()[:120],
        "category": params.get("category", "")[:50],
        "library_code": params.get("library", "")[:12],
        "availability": params.get("availability", ""),
    }


def _filtered_catalogue(books, term, category, library_code, availability):
    books = search_titles(books, term)
    if category:
        books = books.filter(category=category)
    if library_code:
        books = books.filter(library__code=library_code)
    if availability == "available":
        return books.filter(quantity__gt=0)
    if availability == "unavailable":
        return books.filter(quantity=0)
    return books


@never_cache
@require_GET
def home(request):
    books = visible_books(request.user)
    popular = (
        books.annotate(loans=Count("issuedbook"))
        .filter(loans__gt=0)
        .order_by("-loans", "name", "pk")[:5]
    )
    categories = (
        books.exclude(category="")
        .values("category")
        .annotate(total=Count("pk"))
        .order_by("-total", "category")[:8]
    )
    return render(
        request,
        "verse/home.html",
        {
            "featured": books.order_by("-pk")[:5],
            "popular": popular,
            "categories": categories,
        },
    )


@never_cache
@require_GET
def catalogue(request):
    source = visible_books(request.user)
    filters = _catalogue_filters(request.GET)
    sort = request.GET.get("sort", "title")
    books = _filtered_catalogue(source, **filters)
    page = Paginator(books.order_by(*SORTS.get(sort, SORTS["title"])), 15).get_page(
        request.GET.get("page")
    )
    context = {
        "page": page,
        **filters,
        "sort": sort,
        "libraries": visible_libraries(request.user).order_by("name"),
        "categories": source.exclude(category="")
        .order_by("category")
        .values_list("category", flat=True)
        .distinct(),
    }
    template = (
        "verse/catalogue_results.html"
        if request.headers.get("X-Libraverse-Fragment")
        else "verse/catalogue.html"
    )
    return render(request, template, context)


@never_cache
@require_GET
def suggestions(request):
    filters = _catalogue_filters(request.GET)
    if len(filters["term"]) < MIN_SUGGESTION_LENGTH:
        return JsonResponse({"results": []})
    books = _filtered_catalogue(visible_books(request.user), **filters)
    return title_suggestions(books, "book_detail", include_library=True)


def _reader_access(book, user):
    if not user.is_authenticated:
        return False
    return (
        book.library.owner_id == user.pk
        or LibraryMembership.objects.filter(
            library=book.library,
            user=user,
            must_change_password=False,
        ).exists()
    )


@never_cache
@require_GET
def book_match(request):
    prompt = request.GET.get("q", "").strip()[:160]
    library_code = request.GET.get("library", "")[:12]
    available_only = request.GET.get("available") == "1"
    books = visible_books(request.user)
    if library_code:
        books = books.filter(library__code=library_code)
    if available_only:
        books = books.filter(quantity__gt=0)

    matches = recommendations(books, prompt) if prompt else []
    for result in matches:
        book = result["book"]
        result["can_reserve"] = _reader_access(book, request.user)
        result["has_reservation"] = result["can_reserve"] and book.reservations.filter(
            member=request.user, status__in=OPEN
        ).exists()

    return render(
        request,
        "verse/book_match.html",
        {
            "prompt": prompt,
            "library_code": library_code,
            "available_only": available_only,
            "libraries": visible_libraries(request.user).order_by("name", "pk"),
            "matches": matches,
        },
    )


def _reservation_context(book, user, has_access):
    reservation = (
        book.reservations.filter(member=user, status__in=OPEN)
        .select_related("hold")
        .first()
        if has_access
        else None
    )
    return {
        "reservation": reservation,
        "position": queue_position(reservation),
        "queue_size": book.reservations.filter(
            status=Reservation.Status.WAITING, member__isnull=False
        ).count(),
        "can_reserve": has_access,
        "now": timezone.now(),
    }


@never_cache
@require_GET
def book_detail(request, pk):
    books = visible_books(request.user)
    book = get_object_or_404(books, pk=pk)
    access = _reader_access(book, request.user)
    context = {"book": book, **_reservation_context(book, request.user, access)}
    if request.headers.get("X-Libraverse-Fragment") == "reservation":
        return render(request, "verse/reservation.html", context)
    context.update(
        {
            "total_copies": book.copies.exclude(status=BookCopy.Status.RETIRED).count(),
            "related": books.filter(library=book.library, category=book.category)
            .exclude(pk=pk)
            .order_by("name", "pk")[:5]
            if book.category
            else [],
        }
    )
    return render(request, "verse/book_detail.html", context)


@require_POST
@login_required
def join_library(request, code):
    library = get_object_or_404(Library, code=code, public_catalogue=True)
    if library.owner_id != request.user.pk:
        membership, _ = LibraryMembership.objects.get_or_create(
            library=library,
            user=request.user,
            defaults={"must_change_password": False},
        )
        if membership.must_change_password:
            request.session["current_library_id"] = library.pk
            return redirect("force_password_change")
    request.session["current_library_id"] = library.pk
    book_id = request.POST.get("book")
    if (
        book_id
        and book_id.isdigit()
        and Book.objects.filter(library=library, pk=book_id).exists()
    ):
        return redirect("book_detail", pk=book_id)
    return redirect("dashboard")


@require_POST
@login_required
@never_cache
def reservation_action(request, pk):
    book = get_object_or_404(visible_books(request.user), pk=pk)
    if not _reader_access(book, request.user):
        return JsonResponse({"error": "A library membership is required."}, status=403)
    try:
        action = request.POST.get("action")
        if action == "reserve":
            entry = reserve(book.pk, request.user)
            message = (
                "Your hold is ready for pickup."
                if entry.status == Reservation.Status.HELD
                else "You joined the waitlist."
            )
        elif action == "cancel":
            cancelled = cancel_reservation(book.pk, request.user)
            message = (
                "Reservation cancelled."
                if cancelled
                else "Your pickup hold has expired."
            )
        else:
            raise ValueError("Choose a valid reservation action.")
    except (ValueError, Book.DoesNotExist) as error:
        if request.headers.get("X-Libraverse-Fragment"):
            return JsonResponse(
                {"error": str(error) or "This title is unavailable."}, status=400
            )
        messages.error(request, str(error) or "This title is unavailable.")
    else:
        request.session["current_library_id"] = book.library_id
        if request.headers.get("X-Libraverse-Fragment"):
            return JsonResponse({"message": message})
        messages.success(request, message)
    return redirect("book_detail", pk=book.pk)


def member_dashboard(request):
    library = request.library
    loans = (
        IssuedBook.objects.filter(book__library=library)
        .filter(
            Q(borrower=request.user)
            | Q(student__user=request.user, student__library=library)
        )
        .select_related("book", "copy")
        .order_by("-issuedate", "-pk")
    )
    entries = (
        Reservation.objects.filter(book__library=library, member=request.user)
        .select_related(
            "book",
            "hold",
        )
        .order_by("-created_at", "-pk")
    )
    active_page = Paginator(entries.filter(status__in=OPEN), 12).get_page(
        request.GET.get("reservations")
    )
    return render(
        request,
        "verse/member.html",
        {
            "library": library,
            "today": timezone.localdate(),
            "now": timezone.now(),
            "loans": Paginator(loans.filter(returned=False), 15).get_page(
                request.GET.get("active")
            ),
            "history": Paginator(loans.filter(returned=True), 15).get_page(
                request.GET.get("history")
            ),
            "reservations": [(entry, queue_position(entry)) for entry in active_page],
            "reservation_page": active_page,
            "notices": Notification.objects.filter(
                library=library, recipient=request.user
            )[:4],
        },
    )


@library_required
@never_cache
def notifications(request):
    qs = Notification.objects.filter(
        library=request.library, recipient=request.user
    ).select_related("book")
    return render(
        request,
        "verse/notifications.html",
        {
            "library": request.library,
            "page": Paginator(qs, 20).get_page(request.GET.get("page")),
        },
    )


@library_required
@require_POST
def mark_read(request):
    qs = Notification.objects.filter(
        library=request.library, recipient=request.user, read_at__isnull=True
    )
    if request.POST.get("notice") != "all":
        item = get_object_or_404(
            qs,
            pk=request.POST.get("notice")
            if request.POST.get("notice", "").isdigit()
            else 0,
        )
        qs = qs.filter(pk=item.pk)
    qs.update(read_at=timezone.now())
    return redirect("notifications")


@login_required
@require_POST
def switch_library(request, code):
    library = get_object_or_404(
        Library.objects.filter(
            Q(owner=request.user) | Q(memberships__user=request.user)
        ).distinct(),
        code=code,
    )
    request.session["current_library_id"] = library.pk
    request.session["library_id"] = library.pk
    request.session["current_library_name"] = library.name
    copy = BookCopy.objects.filter(
        book__library=library, code=request.POST.get("copy", "")
    ).first()
    return redirect("copy_detail", code=copy.code) if copy else redirect("dashboard")
