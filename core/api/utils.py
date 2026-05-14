"""Helpers for API views."""

from core.models import Consumer


def get_managed_consumer(user):
    """Consumer managed by this user, or None. Never raises if the link is missing."""
    return Consumer.objects.filter(consumer_manager=user).first()
