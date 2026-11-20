"""The credential the Celery task sends to ``/api/update-prom-metrics/``.

The worker and the web process share ``SECRET_KEY``, so the token is derived
from it rather than stored: nothing new to configure, and it changes if the key
is rotated.
"""

import hashlib
import hmac

from django.conf import settings
from rest_framework import permissions

#: Request header carrying the token.
HEADER = "X-MM-Metrics-Token"
#: The same header as Django's ``request.META`` spells it.
META_KEY = "HTTP_X_MM_METRICS_TOKEN"


def metrics_push_token():
    return hmac.new(
        settings.SECRET_KEY.encode(), b"update-prom-metrics", hashlib.sha256
    ).hexdigest()


class HasMetricsPushToken(permissions.BasePermission):
    def has_permission(self, request, view):
        supplied = request.META.get(META_KEY, "")
        return hmac.compare_digest(supplied.encode(), metrics_push_token().encode())
