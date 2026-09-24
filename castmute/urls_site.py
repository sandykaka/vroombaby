"""Root URLconf for castmute.com, where CastMute is the whole site.

Deliberately four lines. It mounts the app's existing URLconf at ``/`` instead of at
``/castmute/``, so there is exactly one list of CastMute routes in the project and no
chance of the two mount points drifting apart.

``include()`` rather than re-declaring the patterns here is what preserves the namespace:
``app_name = 'castmute'`` in castmute/urls.py only creates the ``castmute:`` namespace when
the module is included, not when it is used directly as a root URLconf. Reversing
``castmute:support`` would raise NoReverseMatch if this file simply repeated the paths.

Note what is absent: business.urls and the admin. castmute.com serves the product and
nothing else, which keeps the surface on a public product domain as small as the content.
"""

from django.urls import include, path

urlpatterns = [
    path('', include('castmute.urls')),
]
