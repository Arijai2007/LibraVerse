import functools
import logging

from django import forms
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from apps.core.models import AdminProfile, Library
from apps.members.models import LibraryMembership

logger = logging.getLogger(__name__)


# -------------------- LIBRARY CONTEXT HELPERS --------------------


def get_user_library(user):
    """
    Get the library owned by the user (admin).
    Returns None if user is not an admin or has no library.
    """
    if not user.is_authenticated:
        return None
    try:
        return Library.objects.get(owner=user)
    except Library.DoesNotExist:
        return None
    except Library.MultipleObjectsReturned:
        # Should never happen, but return the first one
        return Library.objects.filter(owner=user).first()


def library_required(view_func):
    """
    Decorator to ensure the logged-in user has a library.
    Redirects to library creation page if not.
    """

    @never_cache
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("adminlogin")

        library_id = request.session.get("current_library_id") or request.session.get(
            "library_id"
        )
        library = Library.objects.filter(pk=library_id).first() if library_id else None
        membership = None
        if library and request.user.pk != library.owner_id:
            membership = LibraryMembership.objects.filter(
                library=library, user=request.user
            ).first()
            if not membership:
                library = None
        if library is None:
            library = get_user_library(request.user)
            membership = None
        if library is None:
            membership = (
                LibraryMembership.objects.filter(user=request.user)
                .select_related("library")
                .order_by("-joined_at")
                .first()
            )
            if membership:
                library = membership.library
        if library is None:
            return redirect("catalogue")

        request.session["current_library_id"] = library.pk
        request.session["is_library_owner"] = request.user.pk == library.owner_id
        if membership and membership.must_change_password:
            return redirect("force_password_change")
        request.library = library
        request.can_circulate = request.user.pk == library.owner_id or bool(
            membership and membership.role == "librarian"
        )
        return view_func(request, *args, **kwargs)

    return wrapper


def staff_required(view_func):
    @library_required
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        if not request.can_circulate:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapper


@never_cache
@require_safe
def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


def home_view(request):
    from apps.books.catalogue import home

    return home(request)


@library_required
def dashboard_view(request):
    from apps.books.catalogue import member_dashboard
    from apps.books.reporting import operations_dashboard

    return (
        operations_dashboard(request)
        if request.can_circulate
        else member_dashboard(request)
    )


def adminclick_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "library/adminclick.html")


# -------------------- USER PROFILE VIEWS --------------------


@library_required
def userprofile_view(request):
    """Show admin's profile and library information."""
    library = request.library
    user = request.user
    admin_profile, _ = AdminProfile.objects.get_or_create(user=user)

    context = {
        "current_user": user,
        "admin_profile": admin_profile,
        "library": library,
    }
    return render(request, "library/userprofile.html", context)


class ProfileForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=20, required=False)
    address = forms.CharField(max_length=100, required=False)
    date_of_birth = forms.DateField(required=False)


@library_required
def update_profile_view(request):
    if request.method != "POST":
        return JsonResponse({"message": "Invalid request method."}, status=405)
    form = ProfileForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"message": "Check the profile fields and try again."}, status=400
        )
    user = request.user
    values = form.cleaned_data.copy()
    email = values.pop("email")
    if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
        return JsonResponse({"message": "Email address is already in use."}, status=400)
    user.email = email
    user.first_name = values.pop("first_name")
    user.last_name = values.pop("last_name")
    with transaction.atomic():
        user.save(update_fields=["email", "first_name", "last_name"])
        AdminProfile.objects.update_or_create(user=user, defaults=values)
    return JsonResponse({"message": "Profile updated successfully."})


@library_required
def update_library_view(request):
    """Update library information (name, etc)."""
    library = request.library
    if request.user != library.owner:
        return JsonResponse(
            {"message": "Only the library owner can update library details."},
            status=403,
        )
    if request.method != "POST":
        return JsonResponse({"message": "Invalid request method."}, status=405)

    try:
        library_name = request.POST.get("library_name", "").strip()

        if not library_name or len(library_name) > 100:
            return JsonResponse(
                {"message": "Enter a library name of up to 100 characters."}, status=400
            )

        library.name = library_name
        library.save(update_fields=["name"])

        return JsonResponse(
            {
                "message": "Library updated successfully.",
                "library_name": library.name,
            }
        )

    except (ValueError, TypeError):
        logger.exception("update_library_view invalid request data")
        return JsonResponse(
            {"message": "The request could not be processed."}, status=400
        )
    except Exception:
        logger.exception("update_library_view unexpected error")
        raise


@library_required
def change_password_view(request):
    """
    Handle AJAX request to change user password.
    """
    if request.method != "POST":
        return JsonResponse({"message": "Invalid request method."}, status=405)

    user = request.user

    try:
        current_password = request.POST.get("current_password", "")
        new_password = request.POST.get("new_password", "")

        # Validation
        if not current_password or not new_password:
            return JsonResponse(
                {"message": "All password fields are required."}, status=400
            )

        # Check current password
        if not user.check_password(current_password):
            return JsonResponse(
                {"message": "Current password is incorrect."}, status=400
            )

        try:
            validate_password(new_password, user)
        except ValidationError as error:
            return JsonResponse({"message": " ".join(error.messages)}, status=400)

        if current_password == new_password:
            return JsonResponse(
                {"message": "New password must be different from current password."},
                status=400,
            )

        # Set new password
        user.set_password(new_password)
        user.save()

        # Update session to prevent automatic logout

        update_session_auth_hash(request, user)

        return JsonResponse({"message": "Password changed successfully."})

    except (ValueError, TypeError):
        logger.exception("change_password_view invalid request data")
        return JsonResponse(
            {"message": "The request could not be processed."}, status=400
        )
    except Exception:
        logger.exception("change_password_view unexpected error")
        raise


@library_required
def upload_profile_photo_view(request):
    """Upload admin's profile photo."""
    if request.method != "POST":
        return JsonResponse({"message": "Invalid request method."}, status=405)

    try:
        if "photo" not in request.FILES:
            return JsonResponse({"message": "No photo file provided."}, status=400)

        photo = request.FILES["photo"]
        user = request.user

        allowed_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
        if photo.content_type not in allowed_types:
            return JsonResponse(
                {"message": "Invalid file type. Please upload an image."}, status=400
            )

        if photo.size > 5 * 1024 * 1024:
            return JsonResponse(
                {"message": "File size must be less than 5MB."}, status=400
            )

        admin_profile, _ = AdminProfile.objects.get_or_create(user=user)

        if admin_profile.photo:
            admin_profile.photo.delete()

        admin_profile.photo = photo
        admin_profile.save()

        return JsonResponse({"message": "Photo uploaded successfully."})

    except (ValueError, TypeError):
        logger.exception("upload_profile_photo_view invalid file or request data")
        return JsonResponse(
            {"message": "The request could not be processed."}, status=400
        )
    except Exception:
        logger.exception("upload_profile_photo_view unexpected error")
        raise


@library_required
def remove_profile_photo(request):
    """Remove admin's profile photo."""
    if request.method != "POST":
        return JsonResponse({"message": "Invalid request method."}, status=405)

    try:
        admin_profile, _ = AdminProfile.objects.get_or_create(user=request.user)

        if admin_profile.photo:
            admin_profile.photo.delete(save=True)

        return JsonResponse({"message": "Photo removed successfully."})

    except (ValueError, TypeError):
        logger.exception("remove_profile_photo invalid request data")
        return JsonResponse(
            {"message": "The request could not be processed."}, status=400
        )
    except Exception:
        logger.exception("remove_profile_photo unexpected error")
        raise
