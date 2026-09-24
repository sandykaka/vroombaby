"""Root URLconf for fw.castmute.com — the firmware update channel, and nothing else.

One route, on purpose. The hostname and path are compiled into every shipped device, and a
device that cannot reach this URL cannot be told where the new one is. So this URLconf
exists to be boring: there is nothing here to redesign, nothing to move, and no way for a
change to the marketing site or a newly mounted app to break updates as a side effect.

Anything else on this host returns 404, which is the correct answer.

If this ever has to change, the constraint is not the server — it is the fleet. The old URL
has to keep answering for as long as any device still points at it, which in practice means
forever.
"""

from django.urls import path

from . import views

urlpatterns = [
    path('manifest.json', views.firmware_manifest, name='firmware_manifest'),
]
