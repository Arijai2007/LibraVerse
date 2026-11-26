from django.db.models import Q

from apps.core.models import Library


def has_staff_access(user):
    """Check whether the account owns or can staff any library."""
    return Library.objects.filter(
        Q(owner=user) | Q(memberships__user=user, memberships__role="librarian")
    ).exists()
