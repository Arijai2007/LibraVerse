from allauth.account.adapter import DefaultAccountAdapter
from django.contrib import messages
from django.shortcuts import redirect

from .access import has_staff_access


class ReaderAccountAdapter(DefaultAccountAdapter):
    def pre_login(self, request, user, **kwargs):
        response = super().pre_login(request, user, **kwargs)
        if response is not None:
            return response
        if has_staff_access(user):
            messages.info(request, "Use Staff sign in for your library account.")
            return redirect("staff_login")
        return None
