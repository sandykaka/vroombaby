"""Firmware release information for the CastMute hardware.

The device is built on a reverse-engineered protocol — YouTube's ad signal is not
published by Google — so an over-the-air update is the only way to fix a unit
already sitting in somebody's living room.

Images themselves are hosted on GitHub Releases rather than served from here. That
keeps megabyte binaries out of git history, and more importantly it means a device
whose survival depends on updates does not also depend on this site being up. This
module owns only the question of *which* release is current, which is worth keeping
here because it can be logged and, later, staged.
"""

import json
import logging
import re
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_RELEASE_FILE = Path(__file__).resolve().parent.parent / 'firmware' / 'current.json'

# A placeholder version means "nothing released yet".
UNRELEASED_VERSION = '0.0.0'

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

# Digits and dots, optionally prefixed with v and optionally carrying a build or
# pre-release suffix. Validated for the same reason the url and digest are: a typo should
# surface in our own logs, not as a fleet-wide misbehaviour. A non-numeric version
# compares as 0 in the firmware, so "abc" would silently mean "never update", while a
# bare "999" would force every unit to download at once.
#
# The suffix is allowed on purpose, to match what cast_version.c actually accepts — it
# skips to the next dot, so "1.0.0-rc1" and "1.0.0+build.5" both compare as 1.0.0.
# Rejecting them here would make release candidates impossible to publish.
_VERSION_RE = re.compile(r'^v?\d+(\.\d+){0,3}([-+][0-9A-Za-z.\-]+)?$')


def release_file():
    """Path to the marker naming the current release.

    Overridable so a deployment can publish without a code change.
    """
    return Path(getattr(settings, 'CASTMUTE_RELEASE_FILE', DEFAULT_RELEASE_FILE))


def current_release():
    """The published release as a dict, or None if there isn't a valid one.

    Returns None rather than raising for every failure mode, because the device
    treats a non-200 as "nothing to do" and will simply ask again later. A bad
    manifest should mean no update, never a broken fleet chasing a URL that cannot
    work.
    """
    marker = release_file()
    try:
        current = json.loads(marker.read_text())
    except FileNotFoundError:
        logger.info('CastMute: %s is absent; no firmware published', marker)
        return None
    except (OSError, ValueError):
        logger.exception('CastMute: %s is unreadable', marker)
        return None

    # Valid JSON that isn't an object would raise AttributeError out of .get() below,
    # turning a typo into a 500 and contradicting the promise made just above.
    if not isinstance(current, dict):
        logger.error('CastMute: %s is not a JSON object', marker)
        return None

    version = str(current.get('version') or '').strip()
    url = str(current.get('url') or '').strip()
    digest = str(current.get('sha256') or '').strip().lower()

    if not version or version == UNRELEASED_VERSION:
        return None

    if not _VERSION_RE.match(version):
        logger.error('CastMute: release version %r is not a dotted number; refusing to '
                     'publish it', version)
        return None

    # Checked here so a typo surfaces in our own logs rather than as every unit in
    # the field failing to download. The firmware also refuses plain HTTP, but by
    # then the manifest has already been served.
    if not url.startswith('https://'):
        logger.error('CastMute: release %s has a non-HTTPS url (%r); refusing to '
                     'publish it', version, url)
        return None

    # Recorded at build time, which is the only moment it is certainly correct.
    if not _SHA256_RE.match(digest):
        logger.error('CastMute: release %s has an invalid sha256 (%r); refusing to '
                     'publish it', version, digest)
        return None

    return {
        'version': version,
        'url': url,
        'sha256': digest,
    }
