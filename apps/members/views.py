import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST

from apps.books.models import IssuedBook, Reservation
from apps.books.services import OPEN
from apps.core.models import Library
from apps.core.views import library_required

from .models import LibraryMembership
from .services import add_member_to_library

logger = logging.getLogger(__name__)


def _owner(request):
    if request.user.pk != request.library.owner_id:
        raise PermissionDenied
    return request.library


def _setup_url(request, user):
    path = reverse(
        "set_invited_password",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )
    return (
        settings.PUBLIC_BASE_URL + path
        if settings.PUBLIC_BASE_URL
        else request.build_absolute_uri(path)
    )


@library_required
def add_member(request):
    library = _owner(request)
    if request.method == "POST" and request.POST.get("add_member"):
        email = request.POST.get("email", "").strip().lower()
        username = request.POST.get("username", "").strip()
        try:
            result = add_member_to_library(request.user, library, email, username)
        except (ValueError, IntegrityError) as error:
            messages.error(
                request,
                str(error)
                if isinstance(error, ValueError)
                else "This account or membership already exists.",
            )
        else:
            if result["is_new"]:
                if settings.INVITATION_EMAIL_ENABLED:
                    setup_url = _setup_url(request, result["user"])
                    try:
                        send_mail(
                            f"LibraVerse invitation to {library.name}",
                            f"You have been invited to {library.name}. Set your password using this one-time link:\n\n{setup_url}",
                            settings.DEFAULT_FROM_EMAIL,
                            [email],
                            fail_silently=False,
                        )
                    except Exception:
                        logger.exception(
                            "LibraVerse invitation delivery failed for library %s",
                            library.pk,
                        )
                        messages.warning(
                            request,
                            "Member added. Email delivery failed. Copy a setup link from Members.",
                        )
                    else:
                        messages.success(
                            request, "Member added and setup link emailed."
                        )
                else:
                    messages.success(
                        request, "Member added. Copy a setup link from Members."
                    )
            else:
                messages.success(
                    request, f"{result['user'].username} was added to this library."
                )
        return redirect("add_member")
    return render(request, "library/add_member.html", {"library": library})


@library_required
def view_members(request):
    library = _owner(request)
    memberships = (
        LibraryMembership.objects.filter(library=library)
        .select_related(
            "user",
            "added_by",
        )
        .order_by("-joined_at")
    )
    return render(
        request,
        "library/view_members.html",
        {"memberships": memberships, "library": library},
    )


@library_required
@require_POST
def member_action(request):
    library = _owner(request)
    membership_id = request.POST.get("membership_id", "")
    action = request.POST.get("action", "remove")
    with transaction.atomic():
        Library.objects.select_for_update(no_key=True).get(pk=library.pk)
        membership = get_object_or_404(
            LibraryMembership,
            pk=membership_id if membership_id.isdecimal() else 0,
            library=library,
        )
        if membership.user_id == library.owner_id:
            raise PermissionDenied
        if action == "setup":
            if not membership.must_change_password:
                raise PermissionDenied
            response = render(
                request,
                "verse/setup_link.html",
                {
                    "library": library,
                    "setup_url": _setup_url(request, membership.user),
                    "member": membership.user,
                },
            )
            response["Cache-Control"] = "no-store"
            response["Referrer-Policy"] = "no-referrer"
            return response
        if action in {"librarian", "member"}:
            membership.role = action
            membership.save(update_fields=["role"])
            library.circulation_activity.create(
                actor=request.user, action=f"Member role changed to {action}"
            )
            messages.success(request, "Member role updated.")
        elif action == "remove":
            user = membership.user
            loans = IssuedBook.objects.filter(
                book__library=library, returned=False
            ).filter(Q(borrower=user) | Q(student__user=user, student__library=library))
            reservations = Reservation.objects.filter(
                book__library=library, member=user, status__in=OPEN
            )
            if loans.exists() or reservations.exists():
                messages.error(
                    request,
                    "Close this member’s loans and reservations before removing access.",
                )
            else:
                membership.delete()
                library.circulation_activity.create(
                    actor=request.user, action="Member removed"
                )
                messages.success(request, f"Removed {user.username} from the library.")
        else:
            raise PermissionDenied
    return redirect("view_members")
