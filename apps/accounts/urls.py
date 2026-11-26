from django.urls import path
from . import views

urlpatterns = [
    path("set-password/<uidb64>/<token>/", views.InvitedPasswordView.as_view(), name="set_invited_password"),
    path("register/", views.register_member, name="register_member"),
    path("adminlogin/", views.AdminLoginView.as_view(), name="adminlogin"),
    path("staff-login/", views.AdminLoginView.as_view(staff_login=True), name="staff_login"),
    path("adminsignup/", views.adminsignup_view, name="adminsignup"),
    path("afterlogin/", views.afterlogin_view, name="afterlogin"),
    path(
        "force-password-change/",
        views.force_password_change,
        name="force_password_change",
    ),
]
