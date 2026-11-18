import csv
from datetime import date, datetime, time
from xml.sax.saxutils import escape

from django import forms
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from apps.core.views import staff_required

from .models import ActivityLog, Book, BookCopy, BookHold, IssuedBook, Reservation
from .services import OPEN

REPORTS = {
    "active": (
        "Active loans",
        ("Title", "Borrower", "Copy", "Issued", "Due", "Status"),
    ),
    "overdue": (
        "Overdue loans",
        ("Title", "Borrower", "Copy", "Issued", "Due", "Status"),
    ),
    "history": (
        "Borrowing history",
        ("Title", "Borrower", "Copy", "Issued", "Due", "Returned", "Status"),
    ),
    "reservations": (
        "Reservations",
        ("Title", "Member", "Requested", "Status", "Copy", "Pickup deadline"),
    ),
    "inventory": (
        "Inventory",
        ("Title", "Author", "Category", "Copy", "Status", "Added"),
    ),
}


def borrower_name(loan):
    if loan.borrower:
        return loan.borrower.get_full_name() or loan.borrower.username
    if loan.student and loan.student.library_id == loan.book.library_id:
        return loan.student.name or loan.enrollment
    return loan.enrollment


def _shift_month(month, offset):
    year, index = divmod(month.year * 12 + month.month - 1 + offset, 12)
    return date(year, index + 1, 1)


def _by_month(qs, field, start):
    model_field = qs.model._meta.get_field(field)
    cutoff = (
        timezone.make_aware(datetime.combine(start, time.min))
        if isinstance(model_field, models.DateTimeField)
        else start
    )
    return dict(
        qs.filter(**{f"{field}__gte": cutoff})
        .annotate(
            month=TruncMonth(field),
        )
        .values("month")
        .annotate(total=Count("pk"))
        .values_list("month", "total")
    )


def operations_dashboard(request):
    library = request.library
    today = timezone.localdate()
    current = today.replace(day=1)
    months = [_shift_month(current, offset) for offset in range(-5, 1)]
    start = months[0]
    books = Book.objects.filter(library=library)
    loans = IssuedBook.objects.filter(book__library=library)
    copies = BookCopy.objects.filter(book__library=library, book__archived=False)
    reservations = Reservation.objects.filter(book__library=library)
    stock = dict(
        copies.values("status")
        .annotate(total=Count("pk"))
        .values_list("status", "total")
    )
    held = BookHold.objects.filter(
        copy__book__library=library,
        ended_at__isnull=True,
        expires_at__gt=timezone.now(),
        reservation__member__isnull=False,
    ).count()
    counts = [
        ("Catalogue titles", books.filter(archived=False).count()),
        (
            "Physical copies",
            sum(n for status, n in stock.items() if status != BookCopy.Status.RETIRED),
        ),
        ("Available copies", stock.get(BookCopy.Status.AVAILABLE, 0)),
        ("Borrowed copies", stock.get(BookCopy.Status.BORROWED, 0)),
        ("Overdue loans", loans.filter(returned=False, return_date__lt=today).count()),
        (
            "Open reservations",
            reservations.filter(status__in=OPEN, member__isnull=False).count(),
        ),
        ("Pickup holds", held),
        ("Active loans", loans.filter(returned=False).count()),
    ]
    fields = [
        ("Issued", _by_month(loans, "issuedate", start)),
        ("Returned", _by_month(loans, "returned_at", start)),
        ("New overdue", _by_month(loans, "overdue_on", start)),
        ("Reservations", _by_month(reservations, "created_at", start)),
    ]
    chart = {
        "labels": [month.strftime("%b %Y") for month in months],
        "series": [
            {
                "name": label,
                "data": [
                    sum(
                        value
                        for key, value in values.items()
                        if key.year == month.year and key.month == month.month
                    )
                    for month in months
                ],
            }
            for label, values in fields
        ],
        "stock": [
            {"label": label, "total": stock.get(key, 0)}
            for key, label in BookCopy.Status.choices
            if key != BookCopy.Status.RETIRED
        ],
    }
    top = list(
        books.annotate(total=Count("issuedbook"))
        .filter(total__gt=0)
        .order_by("-total", "name")[:6]
    )
    genres = list(
        loans.exclude(book__category="")
        .values("book__category")
        .annotate(
            total=Count("pk"),
        )
        .order_by("-total", "book__category")[:6]
    )
    activity = ActivityLog.objects.filter(library=library).select_related(
        "book", "actor"
    )[:12]
    physical_count = counts[1][1]
    return render(
        request,
        "verse/operations.html",
        {
            "library": library,
            "metrics": counts,
            "chart": chart,
            "top": top,
            "genres": genres,
            "activity": activity,
            "utilization": round(
                100 * stock.get(BookCopy.Status.BORROWED, 0) / physical_count
            )
            if physical_count
            else 0,
        },
    )


class ReportForm(forms.Form):
    report = forms.ChoiceField(
        choices=[(key, value[0]) for key, value in REPORTS.items()], required=False
    )
    start = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="From date",
    )
    end = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="To date"
    )
    status = forms.ChoiceField(
        required=False, choices=[("", "Any status")], label="Status"
    )
    category = forms.ChoiceField(required=False, label="Category")
    book = forms.ModelChoiceField(queryset=Book.objects.none(), required=False)
    member = forms.ModelChoiceField(queryset=User.objects.none(), required=False)

    def __init__(self, library, report, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["book"].queryset = Book.objects.filter(library=library).order_by(
            "name"
        )
        self.fields["category"].choices = [("", "Any category")] + [
            (name, name)
            for name in Book.objects.filter(library=library)
            .exclude(category="")
            .order_by(
                "category",
            )
            .values_list("category", flat=True)
            .distinct()
        ]
        if report == "inventory":
            self.fields["status"].choices += list(BookCopy.Status.choices)
            del self.fields["member"]
        elif report == "reservations":
            self.fields["status"].choices += list(Reservation.Status.choices)
            self.fields["member"].queryset = (
                User.objects.filter(reservations__book__library=library)
                .distinct()
                .order_by("username")
            )
        else:
            self.fields["status"].choices += [
                ("active", "On loan"),
                ("overdue", "Overdue"),
                ("returned", "Returned"),
            ]
            self.fields["member"].queryset = (
                User.objects.filter(library_loans__book__library=library)
                .distinct()
                .order_by("username")
            )

    def clean(self):
        result = super().clean()
        if (
            result.get("start")
            and result.get("end")
            and result["start"] > result["end"]
        ):
            self.add_error("end", "The end date must be on or after the start date.")
        return result


def _records(library, report, filters):
    today = timezone.localdate()
    status = filters["status"]
    if report == "inventory":
        qs = BookCopy.objects.filter(book__library=library).select_related("book")
        date_field = "created_at__date"
        if status:
            qs = qs.filter(status=status)
        order = ("book__name", "pk")
    elif report == "reservations":
        qs = Reservation.objects.filter(book__library=library).select_related(
            "book", "member", "hold__copy"
        )
        date_field = "created_at__date"
        if status:
            qs = qs.filter(status=status)
        if filters.get("member"):
            qs = qs.filter(member=filters["member"])
        order = ("-created_at", "-pk")
    else:
        qs = IssuedBook.objects.filter(book__library=library).select_related(
            "book", "borrower", "student", "copy"
        )
        date_field = "issuedate"
        if report in {"active", "overdue"}:
            qs = qs.filter(returned=False)
        if report == "overdue" or status == "overdue":
            qs = qs.filter(returned=False, return_date__lt=today)
        if status == "active":
            qs = qs.filter(returned=False, return_date__gte=today)
        if status == "returned":
            qs = qs.filter(returned=True)
        if filters.get("member"):
            qs = qs.filter(borrower=filters["member"])
        order = ("-issuedate", "-pk")
    for key, lookup in [
        ("start", f"{date_field}__gte"),
        ("end", f"{date_field}__lte"),
        ("category", "book__category"),
        ("book", "book"),
    ]:
        if filters.get(key):
            qs = qs.filter(**{lookup: filters[key]})
    return qs.order_by(*order)


def _date(value):
    return value.isoformat() if value else "—"


def _timestamp(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M %Z") if value else "—"


def _row(report, item, today):
    if report == "inventory":
        return [
            item.book.name,
            item.book.author,
            item.book.category,
            item.code,
            item.get_status_display(),
            _timestamp(item.created_at),
        ]
    if report == "reservations":
        hold = getattr(item, "hold", None)
        return [
            item.book.name,
            item.member.username if item.member else "Deleted account",
            _timestamp(item.created_at),
            item.get_status_display(),
            hold.copy.code if hold else "—",
            _timestamp(hold.expires_at) if hold else "—",
        ]
    status = (
        "Returned"
        if item.returned
        else ("Overdue" if item.return_date < today else "On loan")
    )
    result = [
        item.book_name or item.book.name,
        borrower_name(item),
        item.copy.code if item.copy_id else "—",
        _date(item.issuedate),
        _date(item.return_date),
    ]
    if report == "history":
        result.append(_timestamp(item.returned_at))
    return [*result, status]


def safe_csv_cell(value):
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
        ("\t", "\r", "\n")
    ):
        return "'" + value
    return value


def _pdf(response, title, headings, rows, metadata):
    forest = colors.HexColor("#143D34")
    body = ParagraphStyle(
        "ReportBody", fontSize=9, leading=12, textColor=colors.HexColor("#172321")
    )
    heading = ParagraphStyle(
        "Heading",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=26,
        leading=31,
        textColor=forest,
    )
    label = ParagraphStyle(
        "Label", parent=body, fontName="Helvetica-Bold", textColor=colors.white
    )
    document = SimpleDocTemplate(
        response,
        pagesize=landscape(A4),
        leftMargin=37,
        rightMargin=37,
        topMargin=35,
        bottomMargin=42,
        title=f"LibraVerse - {title}",
    )
    data = [[Paragraph(escape(value), label) for value in headings]]
    for row in rows:
        data.append([Paragraph(escape(str(value)), body) for value in row])
    if len(data) == 1:
        data.append(
            [Paragraph("No matching records.", body)] + [""] * (len(headings) - 1)
        )
    weights = {
        "Title": 1.65,
        "Borrower": 1.20,
        "Member": 1.15,
        "Copy": 1.35,
        "Returned": 1.25,
        "Added": 1.25,
        "Requested": 1.25,
        "Pickup deadline": 1.30,
    }
    widths = [weights.get(label, 1) for label in headings]
    table = LongTable(
        data,
        repeatRows=1,
        colWidths=[document.width * weight / sum(widths) for weight in widths],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), forest),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F8F7F2")],
                ),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DFE5DE")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    def footer(canvas, doc):
        canvas.setStrokeColor(colors.HexColor("#C9A86A"))
        canvas.line(37, 32, 37 + doc.width, 32)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(37, 19, "LibraVerse | Smart Library Management System")
        canvas.drawRightString(37 + doc.width, 19, f"Page {doc.page}")

    story = [Paragraph(escape(title), heading), Spacer(1, 8)]
    story.extend(Paragraph(escape(line), body) for line in metadata)
    story.extend([Paragraph(f"Total records: {len(rows)}", body), Spacer(1, 15), table])
    document.build(story, onFirstPage=footer, onLaterPages=footer)


@staff_required
@require_GET
def reports(request):
    library = request.library
    report = request.GET.get("report") or "active"
    form = ReportForm(library, report, request.GET)
    title, headings = REPORTS.get(report, REPORTS["active"])
    valid = form.is_valid() and report in REPORTS
    records = (
        _records(library, report, form.cleaned_data)
        if valid
        else IssuedBook.objects.none()
    )
    output = request.GET.get("format")
    today = timezone.localdate()
    if output in {"csv", "pdf"} and valid:
        filename = f"libraverse-{report}-{today}"
        rows = (_row(report, item, today) for item in records.iterator(chunk_size=500))
        if output == "csv":
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
            writer = csv.writer(response)
            writer.writerow(headings)
            writer.writerows([safe_csv_cell(value) for value in row] for row in rows)
        else:
            response = HttpResponse(content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
            selected = []
            for key, field in form.fields.items():
                value = form.cleaned_data.get(key)
                if not value or key == "report":
                    continue
                if isinstance(value, Book):
                    value = value.name
                elif isinstance(value, User):
                    value = value.get_username()
                elif isinstance(field, forms.ChoiceField):
                    value = dict(field.choices).get(value, value)
                selected.append(f"{field.label or key.title()}={value}")
            metadata = [
                library.name,
                f"Generated {_timestamp(timezone.now())}",
                "Filters: "
                + (", ".join(selected) if selected else "All records for this report"),
            ]
            _pdf(response, title, headings, list(rows), metadata)
        return response
    page = Paginator(records, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "verse/reports.html",
        {
            "library": library,
            "form": form,
            "title": title,
            "headings": headings,
            "report": report,
            "page": page,
            "rows": [_row(report, item, today) for item in page],
            "valid": valid,
            "report_types": [(key, value[0]) for key, value in REPORTS.items()],
        },
        status=400 if output and not valid else 200,
    )
