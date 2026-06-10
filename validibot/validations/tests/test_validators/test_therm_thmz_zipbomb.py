"""
Regression tests for THMZ zip-bomb protection in the THERM parser.

The THERM validator runs in-process inside the Django/Celery worker and
opens uploaded ``.thmz`` ZIP archives, decompressing a member entirely into
memory. Without an uncompressed-size cap, a tiny high-compression-ratio
archive (a "zip bomb") could decompress to many gigabytes and OOM the
worker. ``parser.MAX_THMZ_UNCOMPRESSED_BYTES`` bounds how much we will
extract from any single member; these tests pin that protection so a future
refactor of ``_extract_xml_from_thmz`` cannot silently remove it.
"""

from __future__ import annotations

import io
import zipfile
from unittest import mock

import pytest
from django.test import TestCase

from validibot.validations.validators.therm.parser import MAX_THMZ_UNCOMPRESSED_BYTES
from validibot.validations.validators.therm.parser import _extract_xml_from_thmz
from validibot.validations.validators.therm.parser import parse_therm_file

# A highly compressible payload: a long run of identical bytes compresses to
# almost nothing in a ZIP but, if read unbounded, inflates back to this full
# size in memory. We make it one byte past the cap so the parser must reject
# it rather than allocate it.
ZIPBOMB_MEMBER_SIZE = MAX_THMZ_UNCOMPRESSED_BYTES + 1


def _make_zipbomb_thmz() -> bytes:
    """Build a small .thmz whose single .thmx member inflates past the cap.

    The on-disk archive stays tiny (a repeated byte deflates extremely well),
    but the declared/actual uncompressed size exceeds
    ``MAX_THMZ_UNCOMPRESSED_BYTES`` — exactly the asymmetry a zip bomb exploits.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("model.thmx", b"A" * ZIPBOMB_MEMBER_SIZE)
    return buf.getvalue()


class ThermThmzZipBombTests(TestCase):
    """Verify oversized THMZ members are rejected without being materialised."""

    def test_oversized_thmz_member_is_rejected(self):
        """An over-cap .thmz member must raise ValueError, not OOM the worker.

        This is the core security guarantee: the parser refuses to decompress
        a member larger than the cap, so a malicious upload cannot exhaust
        worker memory. We assert the archive on disk is small (proving the
        member only *inflates* to a dangerous size) and that parsing it raises
        a clear validation error referencing the limit.
        """
        thmz = _make_zipbomb_thmz()
        # The bomb archive itself is tiny — the danger is purely in inflation.
        assert len(thmz) < MAX_THMZ_UNCOMPRESSED_BYTES
        with pytest.raises(ValueError, match="limit"):
            parse_therm_file(thmz, filename="bomb.thmz")

    def test_lying_header_does_not_let_the_bomb_through(self):
        """A member whose declared size lies must still be refused, not processed.

        ``ZipInfo.file_size`` is attacker-controlled metadata, so the up-front
        size check alone is not enough — an attacker could forge a small
        declared size to slip past it. We simulate that by forging the declared
        size to 0 and confirm the bomb is still *rejected* rather than silently
        decompressed.

        Defence-in-depth note: forging ``file_size`` does not just bypass our
        up-front check, it makes the member inconsistent with the archive's
        stored CRC/size, so Python's own ``zipfile`` integrity check refuses it
        at read time (surfaced here as a ``ValueError``). Either way — our size
        cap on an honest header, or ``zipfile``'s CRC check on a forged one —
        the oversized member never gets materialised. We assert only that a
        ``ValueError`` is raised, because the exact refusal path is not a
        security guarantee; *refusal* is.
        """
        thmz = _make_zipbomb_thmz()

        real_getinfo = zipfile.ZipFile.getinfo

        def lying_getinfo(self, name):
            info = real_getinfo(self, name)
            info.file_size = 0  # forged: claim the member is empty
            return info

        with (
            mock.patch.object(zipfile.ZipFile, "getinfo", lying_getinfo),
            pytest.raises(ValueError, match="THMZ"),
        ):
            _extract_xml_from_thmz(thmz)
