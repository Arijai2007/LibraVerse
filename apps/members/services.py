"""
Library membership management service layer.
Handles core business logic for adding members to libraries.
"""

from django.contrib.auth.models import User
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from apps.core.models import AdminProfile, Library
from apps.members.models import LibraryMembership

__all__ = ["add_member_to_library"]


@transaction.atomic
def add_member_to_library(owner, library, email, username):
    """
    Add a member to a library. Handles both new and existing users.

    Args:
        owner: User instance (library owner)
        library: Library instance
        email: String email address (lowercase)
        username: String username

    Returns:
        dict with keys: 'user', 'membership', 'is_new'
        - is_new: True if user was created, False if existing

    Raises:
        ValueError: If email/username invalid or username already taken
        Exception: If user already member of this library
    """
    email = email.strip().lower()
    username = username.strip()

    # Validate email
    if owner.pk != library.owner_id:
        raise ValueError("Only the library owner can add members.")
    try:
        validate_email(email)
    except ValidationError as error:
        raise ValueError("Please enter a valid email address.") from error

    # Validate username
    if not username:
        raise ValueError("Please enter a username for the new member.")

    try:
        if len(username) > 150:
            raise ValidationError("Username is too long.")
        UnicodeUsernameValidator()(username)
    except ValidationError as error:
        raise ValueError("Enter a valid username.") from error

    Library.objects.select_for_update(no_key=True).get(pk=library.pk)
    existing_user = User.objects.filter(username=username).first()
    if existing_user and existing_user.email.casefold() != email:
        raise ValueError(
            "That username is taken. Use the username and email on the existing account."
        )
    if not existing_user and User.objects.filter(email__iexact=email).exists():
        raise ValueError("This email has an account. Enter its exact username.")

    if existing_user:
        # User exists — check if already member
        if LibraryMembership.objects.filter(
            library=library, user=existing_user
        ).exists():
            raise ValueError(
                f"'{existing_user.username}' ({email}) is already a member "
                "of this library."
            )

        # Add as member (they keep existing credentials)
        AdminProfile.objects.get_or_create(user=existing_user)

        membership = LibraryMembership.objects.create(
            library=library,
            user=existing_user,
            added_by=owner,
            role="member",
            must_change_password=False,
        )

        return {
            "user": existing_user,
            "membership": membership,
            "is_new": False,
        }

    # Create brand-new user
    new_user = User.objects.create_user(email=email, username=username, password=None)

    AdminProfile.objects.get_or_create(user=new_user)

    membership = LibraryMembership.objects.create(
        library=library,
        user=new_user,
        added_by=owner,
        role="member",
        must_change_password=True,
    )

    return {
        "user": new_user,
        "membership": membership,
        "is_new": True,
    }
