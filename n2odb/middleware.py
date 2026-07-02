"""Custom middleware for GRAND Explorer."""
import base64
import hmac

from django.conf import settings
from django.http import HttpResponse


class SoftLaunchGateMiddleware:
    """Gate the entire site behind one shared HTTP Basic Auth credential.

    Controlled entirely by environment variables, so toggling it needs no code
    change and no redeploy of the image:

      * SITE_PASSWORD unset/empty -> site is fully public (middleware no-ops)
      * SITE_PASSWORD set         -> every request must supply Basic Auth

    Username defaults to 'lab' (override with SITE_USERNAME). At public launch,
    just clear SITE_PASSWORD in the Railway variables.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        password = getattr(settings, 'SITE_PASSWORD', '') or ''
        if not password:
            return self.get_response(request)

        username = getattr(settings, 'SITE_USERNAME', 'sahalab') or 'sahalab'
        header = request.META.get('HTTP_AUTHORIZATION', '')
        if header.startswith('Basic '):
            try:
                decoded = base64.b64decode(header.split(' ', 1)[1]).decode('utf-8')
                user_in, _, pass_in = decoded.partition(':')
                # constant-time compare so we don't leak length/prefix via timing
                if (hmac.compare_digest(user_in, username)
                        and hmac.compare_digest(pass_in, password)):
                    return self.get_response(request)
            except Exception:
                pass

        resp = HttpResponse('Private preview — authorization required.', status=401)
        resp['WWW-Authenticate'] = 'Basic realm="GRAND Explorer (private preview)"'
        return resp
