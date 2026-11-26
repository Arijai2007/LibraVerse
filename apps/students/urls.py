from django.urls import path
from . import library_pass, views
from .views_csv import (
    import_students_view,
    export_students_csv_view,
    export_students_pdf_view,
    download_students_csv_sample_view,
)

urlpatterns = [
    path("fine-policy/", library_pass.fine_policy, name="fine_policy"),
    path("<int:pk>/pass/", library_pass.student_pass, name="student_pass"),
    path("pass/<str:token>/", library_pass.student_history, name="student_history"),
    path("pass/<str:token>/qr/", library_pass.student_pass_qr, name="student_pass_qr"),
    path(
        "pass/<str:token>/loans/<int:loan_id>/fine/",
        library_pass.student_fine_action,
        name="student_fine_action",
    ),
    path("addstudent/", views.addstudent_view, name="addstudent"),
    path("studentadded/", views.studentadded_view, name="studentadded"),
    path("viewstudent/", views.viewstudent_view, name="viewstudent"),
    path("deletestudents/", views.delete_students_view, name="deletestudents"),
    path("updatestudents/", views.update_students_view, name="updatestudents"),
    path("import/", import_students_view, name="import_students"),
    path("export/csv/", export_students_csv_view, name="export_students_csv"),
    path("export/pdf/", export_students_pdf_view, name="export_students_pdf"),
    path(
        "import/sample/", download_students_csv_sample_view, name="students_csv_sample"
    ),
]
