import logging
from time import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordResetConfirmView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.core.models import AdminProfile, Library
from apps.members.models import LibraryMembership

from .access import has_staff_access

logger = logging.getLogger(__name__)


class AdminLoginView(View):
    """
    Credential-first login flow.
    Step 1: authenticate username/password.
    Step 2: if user belongs to one library, auto-login.
    Step 3: if user belongs to multiple libraries, show filtered picker.
    """

    template_name = "library/adminlogin.html"
    staff_login = False

    def _clear_pending_login_session(self, request):
        request.session.pop("pending_user_id", None)
        request.session.pop("eligible_library_ids", None)
        request.session.pop("pending_login_at", None)
        request.session.pop("pending_staff_login", None)

    def _render_login(self, request, extra_context=None):
        context = {
            "staff_login": self.staff_login,
            "staff_password_reset_available": settings.INVITATION_EMAIL_ENABLED,
        }
        if extra_context:
            context.update(extra_context)
        return render(request, self.template_name, context)

    def _check_login_role(self, request, user):
        is_staff = has_staff_access(user)
        if is_staff == self.staff_login:
            return None
        self._clear_pending_login_session(request)
        if is_staff:
            messages.info(request, "Use Staff sign in for your library account.")
        else:
            messages.info(request, "Use Reader sign in for your member account.")
        return self._render_login(request)

    def _get_eligible_libraries(self, user):
        return (
            Library.objects.filter(Q(owner=user) | Q(memberships__user=user))
            .distinct()
            .order_by("-created_at")
        )

    def _ensure_admin_profile_and_membership(self, user, library):
        if library.owner == user:
            admin_profile, _ = AdminProfile.objects.get_or_create(user=user)
            if admin_profile.library is None or admin_profile.library != library:
                admin_profile.library = library
                admin_profile.save()
                logger.debug(
                    "adminlogin: linked admin profile %s to library %s",
                    admin_profile.id,
                    library.id,
                )

            LibraryMembership.objects.get_or_create(
                library=library,
                user=user,
                defaults={
                    "role": "owner",
                    "added_by": user,
                    "must_change_password": False,
                },
            )
            return True, None

        if not LibraryMembership.objects.filter(library=library, user=user).exists():
            return False, "You do not have access to this library."

        try:
            user.admin_profile
        except AdminProfile.DoesNotExist:
            AdminProfile.objects.create(user=user)

        return True, None

    def _login_user_to_library(self, request, user, library):
        success, error_message = self._ensure_admin_profile_and_membership(
            user, library
        )
        if not success:
            self._clear_pending_login_session(request)
            messages.error(request, error_message)
            return self._render_login(request)

        is_owner = library.owner == user
        request.session["current_library_id"] = library.id
        request.session["library_id"] = library.id
        request.session["current_library_name"] = library.name
        request.session["is_library_owner"] = is_owner
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        self._clear_pending_login_session(request)
        logger.debug("adminlogin: authenticated and logged in user %s", user.username)
        messages.success(request, f"Welcome back! Logged into {library.name}.")
        return self._success(request, "dashboard")

    def _success(self, request, fallback):
        target = request.GET.get("next", "")
        if target.startswith("/") and url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(target)
        return redirect(fallback)

    def get(self, request):
        if request.user.is_authenticated:
            return self._success(request, "dashboard")

        self._clear_pending_login_session(request)
        return self._render_login(request)

    def post(self, request):
        if request.user.is_authenticated:
            return self._success(request, "dashboard")

        pending_user_id = request.session.get("pending_user_id")
        selected_library = request.POST.get("selected_library")

        if (
            pending_user_id
            and (
                time() - request.session.get("pending_login_at", 0) > 300
                or request.session.get("pending_staff_login") != self.staff_login
            )
        ):
            self._clear_pending_login_session(request)
            messages.error(request, "Please sign in again to select a library.")
            return self._render_login(request)
        if pending_user_id:
            if not selected_library:
                eligible_library_ids = request.session.get("eligible_library_ids", [])
                eligible_libraries = Library.objects.filter(
                    id__in=eligible_library_ids
                ).order_by("-created_at")
                messages.error(
                    request, "Please select one of your libraries to continue."
                )
                return self._render_login(
                    request,
                    {
                        "eligible_libraries": eligible_libraries,
                        "multi_library_message": "You belong to multiple libraries. Please select one to continue.",
                    },
                )

            try:
                user = User.objects.get(id=pending_user_id, is_active=True)
            except User.DoesNotExist:
                self._clear_pending_login_session(request)
                messages.error(
                    request, "Unable to continue login. Please sign in again."
                )
                return self._render_login(request)

            role_error = self._check_login_role(request, user)
            if role_error is not None:
                return role_error

            try:
                library_id = int(selected_library)
            except (TypeError, ValueError):
                self._clear_pending_login_session(request)
                messages.error(request, "Invalid library selection.")
                return self._render_login(request)

            eligible_library_ids = request.session.get("eligible_library_ids", [])
            if library_id not in eligible_library_ids:
                self._clear_pending_login_session(request)
                messages.error(request, "Invalid library selection.")
                return self._render_login(request)

            try:
                library = Library.objects.get(id=library_id)
            except Library.DoesNotExist:
                self._clear_pending_login_session(request)
                messages.error(request, "Selected library not found.")
                return self._render_login(request)

            return self._login_user_to_library(request, user, library)

        username = request.POST.get("username")
        password = request.POST.get("password")
        self._clear_pending_login_session(request)

        if not username or not password:
            messages.error(request, "Please enter your username and password.")
            return self._render_login(request)

        user = authenticate(request, username=username, password=password)
        if user is None:
            messages.error(request, "Invalid username or password.")
            return self._render_login(request)

        role_error = self._check_login_role(request, user)
        if role_error is not None:
            return role_error

        library_qs = self._get_eligible_libraries(user)
        library_count = library_qs.count()

        if library_count == 0:
            auth_login(
                request, user, backend="django.contrib.auth.backends.ModelBackend"
            )
            return self._success(request, "dashboard")

        if library_count == 1:
            return self._login_user_to_library(request, user, library_qs.first())

        eligible_libraries = list(library_qs)
        request.session.cycle_key()
        request.session["pending_user_id"] = user.id
        request.session["pending_login_at"] = time()
        request.session["pending_staff_login"] = self.staff_login
        request.session["eligible_library_ids"] = [lib.id for lib in eligible_libraries]

        return self._render_login(
            request,
            {
                "eligible_libraries": eligible_libraries,
                "multi_library_message": "You belong to multiple libraries. Please select one to continue.",
            },
        )


def adminsignup_view(request):
    """Handle admin signup for creating a new library."""
    from apps.core.services import create_library_with_owner

    # --- GOOGLE / already-logged-in path ---
    if request.user.is_authenticated:
        from .forms import CreateLibraryNameForm

        form = CreateLibraryNameForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                user = request.user
                library_name = form.cleaned_data["library_name"]

                # Use service layer
                result = create_library_with_owner(user, library_name)
                library = result["library"]

                # Set session keys
                request.session["library_id"] = library.id
                request.session["current_library_id"] = library.id
                request.session["current_library_name"] = library.name
                request.session["is_library_owner"] = True

                messages.success(
                    request, f"Library '{library.name}' created successfully!"
                )
                return redirect("dashboard")
            except (IntegrityError, ValidationError) as e:
                logger.exception("adminsignup google-user validation error")
                messages.error(request, f"An error occurred: {str(e)}")
            except Exception:
                logger.exception("adminsignup google-user unexpected error")
                raise

        return render(
            request, "library/adminsignup.html", {"form": form, "google_user": True}
        )

    # --- Normal email/password signup path ---
    from .forms import CreateLibraryForm

    form = CreateLibraryForm()
    if request.method == "POST":
        form = CreateLibraryForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=form.cleaned_data["username"],
                        email=form.cleaned_data["email"],
                        password=form.cleaned_data["password1"],
                    )
                    library = create_library_with_owner(
                        user, form.cleaned_data["library_name"]
                    )["library"]

                # Set session keys
                auth_login(
                    request, user, backend="django.contrib.auth.backends.ModelBackend"
                )
                request.session["library_id"] = library.id
                request.session["current_library_id"] = library.id
                request.session["current_library_name"] = library.name
                request.session["is_library_owner"] = True

                messages.success(
                    request, f"Library '{library.name}' created successfully!"
                )
                return redirect("dashboard")
            except (IntegrityError, ValidationError) as e:
                logger.exception("adminsignup validation error")
                messages.error(request, f"An error occurred: {str(e)}")
            except Exception:
                logger.exception("adminsignup unexpected error")
                raise

    return render(request, "library/adminsignup.html", {"form": form})


# -------------------- AFTER LOGIN --------------------


def afterlogin_view(request):
    """Set the active library after social login and open the workspace."""
    if not request.user.is_authenticated:
        return redirect("adminlogin")

    # Check if user has a library
    from apps.core.views import get_user_library

    library = get_user_library(request.user)
    if not library:
        membership = (
            LibraryMembership.objects.filter(user=request.user)
            .select_related("library")
            .first()
        )
        if membership:
            library = membership.library

    if library:
        admin_profile, _ = AdminProfile.objects.get_or_create(user=request.user)
        if admin_profile.library is None:
            admin_profile.library = library
            admin_profile.save()

        request.session["current_library_id"] = library.id
        request.session["library_id"] = library.id
        request.session["current_library_name"] = library.name
        request.session["is_library_owner"] = request.user == library.owner
        messages.success(request, f"Welcome back to {library.name}!")

    return redirect("dashboard")


@login_required
def force_password_change(request):
    """Force password change for new members."""
    library_id = request.session.get("current_library_id") or request.session.get(
        "library_id"
    )
    membership = LibraryMembership.objects.filter(
        user=request.user, library_id=library_id
    ).first()
    if not membership or not membership.must_change_password:
        return redirect("dashboard")

    if request.method == "POST":
        from django.contrib.auth import update_session_auth_hash
        from django.contrib.auth.password_validation import validate_password

        new_password = request.POST.get("new_password", "")
        try:
            if new_password != request.POST.get("confirm_password", ""):
                raise ValidationError("Passwords must match.")
            validate_password(new_password, request.user)
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
        else:
            from django.db import transaction

            with transaction.atomic():
                request.user.set_password(new_password)
                request.user.save(update_fields=["password"])
                membership.must_change_password = False
                membership.save(update_fields=["must_change_password"])
            update_session_auth_hash(request, request.user)
            return redirect("dashboard")

    return render(
        request, "library/force_password_change.html", {"membership": membership}
    )


def register_member(request):
    from django.db import transaction

    from .forms import MemberRegistrationForm

    libraries = Library.objects.filter(public_catalogue=True).order_by("name")
    selected = (
        request.POST.get("library", "")
        if request.method == "POST"
        else request.GET.get("library", "")
    )
    form = MemberRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        library = libraries.filter(code=selected).first() if selected else None
        if selected and not library:
            form.add_error(None, "Choose a library that accepts public registrations.")
        else:
            try:
                with transaction.atomic():
                    user = form.save()
                    if library:
                        LibraryMembership.objects.create(
                            library=library,
                            user=user,
                            must_change_password=False,
                        )
            except IntegrityError:
                form.add_error("username", "This username is already taken.")
            else:
                auth_login(
                    request, user, backend="django.contrib.auth.backends.ModelBackend"
                )
                if library:
                    request.session["current_library_id"] = library.pk
                messages.success(request, "Your LibraVerse account is ready.")
                return redirect("dashboard")
    return render(
        request,
        "verse/register.html",
        {
            "form": form,
            "libraries": libraries,
            "selected_library": selected,
        },
    )


class InvitedPasswordView(PasswordResetConfirmView):
    template_name = "verse/invited_password.html"
    success_url = reverse_lazy("adminlogin")

    def get_success_url(self):
        if has_staff_access(self.user):
            return reverse_lazy("staff_login")
        return super().get_success_url()

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        LibraryMembership.objects.filter(
            user=self.user, must_change_password=True
        ).update(
            must_change_password=False,
        )
        messages.success(self.request, "Password set. You can now sign in.")
        return response
