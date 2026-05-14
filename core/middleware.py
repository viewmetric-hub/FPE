from django.utils.deprecation import MiddlewareMixin


class RequestRoleMiddleware(MiddlewareMixin):
    """
    Convenience middleware: for authenticated requests, attach `request.user_role`.

    DRF views will still enforce permissions using `request.user.role`.
    """

    def process_request(self, request):
        role = getattr(getattr(request, 'user', None), 'role', None)
        request.user_role = role
        return None

