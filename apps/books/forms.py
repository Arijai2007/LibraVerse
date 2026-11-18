from datetime import timedelta

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.students.models import StudentExtra

from .models import Book


class BookForm(forms.ModelForm):
    isbn = forms.CharField(required=False, max_length=30, label="ISBN")
    category = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "lms-input",
                "list": "category-options",
                "placeholder": "Select or type a category",
                "autocomplete": "off",
            }
        ),
    )
    language = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "lms-input",
                "list": "language-options",
                "placeholder": "Select or type a language",
                "autocomplete": "off",
            }
        ),
    )

    quantity = forms.IntegerField(
        label="Initial physical copies", min_value=0, max_value=500
    )

    class Meta:
        model = Book
        fields = [
            "name",
            "quantity",
            "author",
            "category",
            "language",
            "isbn",
            "description",
            "cover",
        ]

    def clean_isbn(self):
        import re

        value = re.sub(r"[\s-]", "", self.cleaned_data["isbn"]).upper()
        if value and not re.fullmatch(r"(?:[0-9]{9}[0-9X]|[0-9]{13})", value):
            raise forms.ValidationError("Enter a 10- or 13-character ISBN.")
        return value

    def clean_cover(self):
        cover = self.cleaned_data["cover"]
        if getattr(cover, "image", None) and (
            cover.size > 5_000_000 or cover.image.format not in {"JPEG", "PNG", "WEBP"}
        ):
            raise forms.ValidationError("Upload a JPEG, PNG or WebP image under 5 MB.")
        return cover


class TitleForm(BookForm):
    class Meta(BookForm.Meta):
        fields = [
            "name",
            "author",
            "category",
            "language",
            "isbn",
            "description",
            "cover",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("quantity", None)


class IssuedBookForm(forms.Form):
    book = forms.ModelChoiceField(
        queryset=Book.objects.filter(quantity__gt=0),
        empty_label="Select Book",
        label="Book Name",
        widget=forms.Select(attrs={"class": "form-control select2"}),
    )
    student = forms.ModelChoiceField(
        queryset=StudentExtra.objects.all(),
        empty_label="Select Student",
        label="Student",
        widget=forms.Select(attrs={"class": "form-control select2"}),
    )
    return_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="Return Date",
    )

    def __init__(self, library, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # compute default return date at form instantiation (fixes class-level evaluation bug)
        self.fields["return_date"].initial = timezone.localdate() + timedelta(
            days=max(1, settings.LIBRAVERSE_LOAN_DAYS)
        )
        self.fields["book"].queryset = Book.objects.filter(
            library=library, quantity__gt=0, archived=False
        )
        self.fields["student"].queryset = StudentExtra.objects.filter(library=library)
        self.fields["book"].label_from_instance = lambda obj: (
            f"{obj.name} (Available: {obj.quantity})"
        )
        self.fields["student"].label_from_instance = lambda obj: (
            f"{obj.name} - {obj.enrollment}"
        )
