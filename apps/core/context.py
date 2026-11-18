from django.conf import settings
from django.db.models import Q

from .models import Library


def library_navigation(request):
    from apps.books.models import Notification

    library = getattr(request, "library", None)
    unread = 0
    if library and request.user.is_authenticated:
        unread = Notification.objects.filter(
            library=library,
            recipient=request.user,
            read_at__isnull=True,
        ).count()
    return {
        "can_circulate": getattr(request, "can_circulate", False),
        "unread_notifications": unread,
        "google_oauth_enabled": settings.GOOGLE_OAUTH_ENABLED,
        "workspaces": list(
            Library.objects.filter(
                Q(owner=request.user) | Q(memberships__user=request.user),
            )
            .distinct()
            .order_by("name")
        )
        if library and request.user.is_authenticated
        else [],
    }
