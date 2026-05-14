from rest_framework.permissions import BasePermission


class IsPlatformAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, 'role', None) == request.user.Role.PLATFORM_ADMIN)


class IsConsumerManager(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, 'role', None) == request.user.Role.CONSUMER_MANAGER)


class IsPlantUser(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, 'role', None) == request.user.Role.PLANT_USER)


class IsGenerator(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, 'role', None) == request.user.Role.GENERATOR)


class IsConsumerManagerOrPlatformAdmin(BasePermission):
    def has_permission(self, request, view):
        role = getattr(request.user, 'role', None)
        return role in (request.user.Role.CONSUMER_MANAGER, request.user.Role.PLATFORM_ADMIN)

