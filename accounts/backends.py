from django.contrib.auth.backends import ModelBackend


class LockAwareModelBackend(ModelBackend):
    """Standard username/password auth that refuses locked accounts."""

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and not getattr(user, "is_locked", False)
