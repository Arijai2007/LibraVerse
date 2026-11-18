from django.contrib import admin

from .models import (
    ActivityLog,
    Book,
    BookCopy,
    BookHold,
    IssuedBook,
    Notification,
    Reservation,
)
from .services import audit, initialize_title, lock_title, sync_taxonomy


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ["name", "author", "category", "language", "quantity", "library"]
    search_fields = ["name", "author", "isbn"]
    list_filter = ["category", "language"]
    readonly_fields = ["archived", "author_record", "category_record"]

    def get_readonly_fields(self, request, obj=None):
        return (
            [*self.readonly_fields, "quantity", "library"]
            if obj
            else self.readonly_fields
        )

    def save_model(self, request, obj, form, change):
        if change:
            locked = lock_title(obj.pk)
            obj.quantity = locked.quantity
        super().save_model(request, obj, form, change)
        if change:
            sync_taxonomy(obj)
            audit(obj, "Title updated in Django admin", request.user)
        else:
            initialize_title(obj, request.user)

    def has_delete_permission(self, request, obj=None):
        return False


class CirculationHistoryAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IssuedBook)
class IssuedBookAdmin(CirculationHistoryAdmin):
    list_display = ["enrollment", "book_name", "issuedate", "return_date", "returned"]
    list_filter = ["returned"]


admin.site.register(
    [BookCopy, BookHold, Reservation, Notification, ActivityLog],
    CirculationHistoryAdmin,
)
