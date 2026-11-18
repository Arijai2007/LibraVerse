from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password


# -------------------- CREATE LIBRARY FORM --------------------
class CreateLibraryForm(forms.Form):
    library_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"placeholder": "e.g. City Public Library"}),
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "Choose a username"}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "your@email.com"})
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Create a password", "id": "id_password1"}
        ),
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(
            attrs={"placeholder": "Repeat password", "id": "id_password2"}
        ),
    )

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("This username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("This email is already registered.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("password1")
        p2 = cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Passwords do not match.")
        if p1:
            try:
                validate_password(
                    p1,
                    User(
                        username=cleaned_data.get("username", ""),
                        email=cleaned_data.get("email", ""),
                    ),
                )
            except forms.ValidationError as error:
                self.add_error("password1", error)
        return cleaned_data


class CreateLibraryNameForm(forms.Form):
    library_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"placeholder": "e.g. City Public Library"}),
    )


# -------------------- ADMIN LOGIN FORM --------------------
class AdminLoginForm(forms.Form):
    """Simple login form for use with AdminLoginView (library + username + password)"""

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"placeholder": "Your username"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Your password"}),
    )


class MemberRegistrationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        fields = ("username",)
