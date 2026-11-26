from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.books.models import IssuedBook
from apps.books.services import settle_fine, student_loans
from apps.core.models import Library
from apps.core.qr import public_link, qr_label
from apps.core.views import staff_required, with_library_context
from apps.members.models import LibraryMembership

from .models import StudentExtra


class FinePolicyForm(forms.ModelForm):
    class Meta:
        model = Library
        fields = ["fine_daily_rate", "fine_grace_days"]
        labels = {
            "fine_daily_rate": "Daily fine (₹)",
            "fine_grace_days": "Grace period (days)",
        }
        help_texts = {"fine_daily_rate": "Set to 0 to disable fines for new loans."}


def _student_from_pass(token, library=None):
    try:
        pk = signing.Signer(salt="libraverse.student-pass").unsign(token)
    except signing.BadSignature as error:
        raise Http404 from error
    students = StudentExtra.objects.select_related("library")
    if library is not None:
        students = students.filter(library=library)
    return get_object_or_404(students, pk=pk)


@staff_required
@require_GET
def student_pass(request, pk):
    student = get_object_or_404(StudentExtra, pk=pk, library=request.library)
    token = signing.Signer(salt="libraverse.student-pass").sign(student.pk)
    return redirect("student_history", token=token)


@login_required(login_url="staff_login")
@with_library_context
@require_http_methods(["GET", "POST"])
def student_history(request, token):
    student = _student_from_pass(token)
    library = student.library
    active_library = getattr(request, "library", None)
    if active_library == library:
        if not request.can_circulate:
            raise PermissionDenied
    else:
        is_owner = library.owner_id == request.user.pk
        if not is_owner:
            membership = get_object_or_404(
                LibraryMembership, library=library, user=request.user
            )
            if membership.role != "librarian":
                raise PermissionDenied
        if request.method == "GET":
            return render(
                request, "student/open_pass_workspace.html", {"pass_library": library}
            )
        request.session.update(
            {
                "current_library_id": library.pk,
                "library_id": library.pk,
                "current_library_name": library.name,
                "is_library_owner": is_owner,
            }
        )
    if request.method == "POST":
        return redirect("student_history", token=token)

    loans = (
        student_loans(student).select_related("book", "copy").order_by("-issuedate", "-pk")
    )
    page = Paginator(loans, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "student/library_pass.html",
        {
            "library": library,
            "student": student,
            "token": token,
            "page": page,
            "pass_url": _pass_url(request, token),
        },
    )


@staff_required
@require_GET
def student_pass_qr(request, token):
    _student_from_pass(token, library=request.library)
    return qr_label(_pass_url(request, token))


def _pass_url(request, token):
    return public_link(request, reverse("student_history", args=[token]))


@staff_required
@require_POST
def student_fine_action(request, token, loan_id):
    student = _student_from_pass(token, library=request.library)
    try:
        settle_fine(loan_id, student, request.POST.get("action"), request.user)
    except IssuedBook.DoesNotExist as error:
        raise Http404 from error
    except ValueError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Fine status updated.")
    return redirect("student_history", token=token)


@staff_required
@require_http_methods(["GET", "POST"])
def fine_policy(request):
    library = request.library
    if library.owner_id != request.user.pk:
        raise PermissionDenied
    form = FinePolicyForm(
        request.POST if request.method == "POST" else None, instance=library
    )
    if request.method == "POST" and form.is_valid():
        library.save(update_fields=["fine_daily_rate", "fine_grace_days"])
        messages.success(request, "Fine policy saved for new loans.")
        return redirect("fine_policy")
    return render(request, "student/fine_policy.html", {"form": form, "library": library})
