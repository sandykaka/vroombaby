import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from castmute.services import firmware_service

logger = logging.getLogger(__name__)


def marketing(request):
    return render(request, 'castmute/marketing.html')


def support(request):
    return render(request, 'castmute/support.html')


def privacy(request):
    return render(request, 'castmute/privacy.html')


@require_GET
def firmware_manifest(request):
    """What the hardware should be running, and where to get it.

    Images live on GitHub Releases; this only says which one is current. A non-200
    means "nothing to do" as far as the device is concerned, so having no release is
    a 404 rather than an error.
    """
    release = firmware_service.current_release()
    if release is None:
        return JsonResponse({'error': 'No release published'}, status=404)

    # Logged because it is the only visibility into a fleet that otherwise never
    # phones home: how many units exist, and what they are running.
    logger.info('CastMute: manifest served, version %s to %s',
                release['version'], request.META.get('REMOTE_ADDR', '?'))

    return JsonResponse(release)
