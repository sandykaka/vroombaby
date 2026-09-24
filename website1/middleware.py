"""Choose the URLconf from the request's hostname.

Until now every domain in ALLOWED_HOSTS — vroombaby.com, schoolconvo.com,
coffeewithexpert.com — resolved through the same ROOT_URLCONF and served the identical
site. They were aliases, not separate sites. CastMute needs to be a real second site:
castmute.com should serve the product page at its root, not at /castmute/.

Setting ``request.urlconf`` is the stock Django mechanism for that, with no third-party
package involved. Django's handler calls ``set_urlconf(request.urlconf)`` before resolving,
so ``reverse()`` and ``{% url %}`` use it too — which is why the same
``{% url 'castmute:support' %}`` in one template produces ``/castmute/support/`` on
vroombaby.com and ``/support/`` on castmute.com, with no change to the template.

Fails open on purpose. A hostname that is not in the map leaves ``request.urlconf`` unset,
so Django falls back to ROOT_URLCONF and every existing domain behaves exactly as it did
before this file existed. There is no code path here that can change an existing site's
routing.
"""

from django.core.exceptions import DisallowedHost

HOST_URLCONFS = {
    'castmute.com': 'castmute.urls_site',
    'www.castmute.com': 'castmute.urls_site',

    # Firmware updates only, and nothing else.
    #
    # This hostname is compiled into shipped devices and can never move — a device cannot
    # be told a new address by the mechanism it can no longer reach. Giving it a URLconf
    # containing exactly one route means a redesign of the marketing site, or a new app
    # mounted at the root, cannot take the fleet's update channel down as a side effect.
    'fw.castmute.com': 'castmute.urls_firmware',
}


class HostUrlconfMiddleware:
    """Point CastMute's own domains at CastMute's own URLconf."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            # get_host() rather than HTTP_HOST directly, so USE_X_FORWARDED_HOST is
            # honoured the same way the rest of Django honours it.
            host = request.get_host()
        except DisallowedHost:
            # Not this middleware's decision to make. Carry on and let Django reject it
            # where it normally would, with the error message it normally gives.
            return self.get_response(request)

        # get_host() includes the port, which is present in development
        # (castmute.com:8000). An IPv6 literal such as [::1]:8000 mangles under this
        # naive split, but no such host can match the map above, so it falls through to
        # the default — which is the correct outcome anyway.
        host = host.partition(':')[0].lower()

        urlconf = HOST_URLCONFS.get(host)
        if urlconf is not None:
            request.urlconf = urlconf

        return self.get_response(request)
