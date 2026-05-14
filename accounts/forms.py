from django import forms

from accounts.models import UserProfile


class ProfileForm(forms.ModelForm):
    """Logo upload; name and email are updated via ``CustomUser`` / ``MeView``."""

    class Meta:
        model = UserProfile
        fields = ['logo']
