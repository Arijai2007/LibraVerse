from django.urls import path
from . import catalogue, inventory, reporting, views
from .views_csv import (
    import_books_view,
    export_books_csv_view,
    export_books_pdf_view,
    download_books_csv_sample_view,
)

urlpatterns = [
    path("titles/<int:pk>/", catalogue.book_detail, name="book_detail"),
    path("titles/<int:pk>/reservation/", catalogue.reservation_action, name="reservation_action"),
    path("inventory/", inventory.inventory, name="inventory"),
    path("inventory/<int:pk>/", inventory.title_inventory, name="title_inventory"),
    path("inventory/references/", inventory.taxonomy, name="taxonomy"),
    path("inventory/visibility/", inventory.catalogue_visibility, name="catalogue_visibility"),
    path("circulation/", inventory.circulation_desk, name="circulation_desk"),
    path("copies/<str:code>/", inventory.copy_detail, name="copy_detail"),
    path("copies/<str:code>/action/", inventory.copy_action, name="copy_action"),
    path("copies/<str:code>/qr/", inventory.copy_qr, name="copy_qr"),
    path("reports/", reporting.reports, name="reports"),
    path("addbook/", views.addbook_view, name="addbook"),
    path("viewbook/", views.viewbook_view, name="viewbook"),
    path("issuebook/", views.issuebook_view, name="issuebook"),
    path("viewissuedbook/", views.viewissuedbook_view, name="viewissuedbook"),
    path("deletebooks/", views.delete_books_view, name="deletebooks"),
    path("updatebooks/", views.update_books_view, name="updatebooks"),
    path(
        "return-issued-book/", views.return_issued_book_view, name="return_issued_book"
    ),
    path(
        "update-issued-books/",
        views.update_issued_books_view,
        name="update_issued_books",
    ),
    path("import/", import_books_view, name="import_books"),
    path("export/csv/", export_books_csv_view, name="export_books_csv"),
    path("export/pdf/", export_books_pdf_view, name="export_books_pdf"),
    path("import/sample/", download_books_csv_sample_view, name="books_csv_sample"),
]
