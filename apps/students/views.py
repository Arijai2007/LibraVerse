import json
import logging

from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import redirect, render

from apps.books.models import IssuedBook
from apps.core.models import Library
from apps.core.views import staff_required

from .filters import StudentFilter
from .forms import StudentExtraForm
from .models import StudentExtra

logger = logging.getLogger(__name__)


# -------------------- STUDENT MANAGEMENT VIEWS --------------------


@staff_required
def addstudent_view(request):
    """Add a new student to the current library."""
    library = request.library
    form = StudentExtraForm(library)
    if request.method == "POST":
        form = StudentExtraForm(library, request.POST)
        if form.is_valid():
            student = form.save(commit=False)
            student.library = library
            try:
                with transaction.atomic():
                    student.save()
            except IntegrityError:
                form.add_error(
                    "enrollment", "This reader ID is already used in the library."
                )
            else:
                return redirect("studentadded")
    return render(
        request, "student/addstudent.html", {"form": form, "library": library}
    )


@staff_required
def studentadded_view(request):
    return render(request, "student/studentadded.html", {"library": request.library})


@staff_required
def viewstudent_view(request):
    """View all students in the current library."""
    library = request.library
    # Get students from current library only, sorted by name A-Z
    students = StudentExtra.objects.filter(library=library).order_by("name")

    # Search functionality
    query = request.GET.get("q", "").strip()
    if query:
        students = students.filter(
            Q(name__icontains=query) | Q(enrollment__icontains=query)
        )

    # Initialize filter - use StudentFilter directly
    student_filter = StudentFilter(request.GET, queryset=students)
    students = student_filter.qs

    # Pagination (10 per page)
    paginator = Paginator(students, 10)
    page = request.GET.get("page")

    try:
        students = paginator.page(page)
    except PageNotAnInteger:
        students = paginator.page(1)
    except EmptyPage:
        students = paginator.page(paginator.num_pages)

    return render(
        request,
        "student/viewstudent.html",
        {
            "students": students,
            "filter": student_filter,
            "query": query,
            "gender_choices": StudentExtra.GENDER_CHOICES,
            "library": library,
        },
    )


@staff_required
def delete_students_view(request):
    """Delete students from current library."""
    library = request.library
    if request.method == "POST":
        selected_students = [
            value
            for value in request.POST.getlist("selected_students")
            if value.isdecimal()
        ]
        if selected_students:
            with transaction.atomic():
                Library.objects.select_for_update(no_key=True).get(pk=library.pk)
                records = StudentExtra.objects.filter(
                    id__in=selected_students, library=library
                )
                linked_borrowers = IssuedBook.objects.filter(
                    book__library=library, student__isnull=True, borrower__isnull=False
                ).values("borrower_id")
                if records.filter(
                    Q(issuedbook__isnull=False) | Q(user_id__in=linked_borrowers)
                ).exists():
                    messages.error(
                        request,
                        "Reader records with circulation history must be retained.",
                    )
                    return redirect("viewstudent")
                deleted_count, _ = records.delete()
            messages.success(
                request, f"{deleted_count} Student(s) deleted successfully!"
            )
        else:
            messages.warning(request, "No student selected for deletion.")
        return redirect("viewstudent")
    return redirect("viewstudent")


@staff_required
def update_students_view(request):
    """Update students in current library."""
    library = request.library
    if request.method == "POST":
        try:
            students_data = json.loads(request.POST.get("students_data", "[]"))
            with transaction.atomic():
                Library.objects.select_for_update(no_key=True).get(pk=library.pk)
                for data in students_data:
                    student = StudentExtra.objects.get(
                        pk=int(data["id"]), library=library
                    )
                    form = StudentExtraForm(library, data, instance=student)
                    if not form.is_valid():
                        raise ValueError
                    form.save()
        except (
            ValueError,
            TypeError,
            KeyError,
            StudentExtra.DoesNotExist,
            IntegrityError,
        ):
            messages.error(request, "Check the reader records before saving.")
        else:
            messages.success(request, "Reader records updated.")
    return redirect("viewstudent")
