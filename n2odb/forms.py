"""Account forms.

Two small forms, both leaning on Django's built-in validation/CSRF rather than
hand-rolled handling:

* ``SignupExtraForm`` — extra fields grafted onto allauth's signup (first/last name),
  wired via ``ACCOUNT_SIGNUP_FORM_CLASS``. allauth calls ``signup()`` after it creates
  the user.
* ``NameForm`` — a ModelForm for editing the name on the My Account page.

Email and password changes are NOT here: those go through allauth's hardened views
(reverification of a new email, current-password required on change), which we don't
reimplement.
"""
from __future__ import annotations

from django import forms

from .models import User


class SignupExtraForm(forms.Form):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Last name")

    def signup(self, request, user):
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.save(update_fields=["first_name", "last_name"])


class NameForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]
        labels = {"first_name": "First name", "last_name": "Last name"}

    # Names are how a curator attributes a dataset — required, not blank.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.required = True
