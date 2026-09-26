import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()
log = logging.getLogger("web.tiktok_verification")

VERIFICATION_FILENAME = "tiktokwVtAGGjV61utxKvtT5HcHCXtGSNUGZ67.txt"
VERIFICATION_CONTENT = "tiktok-developers-site-verification=wVtAGGjV61utxKvtT5HcHCXtGSNUGZ67"
# Repository root: web/routes/<this file> -> parents[2]
_VERIFICATION_FILE = Path(__file__).resolve().parents[2] / VERIFICATION_FILENAME


@router.get(
    f"/{VERIFICATION_FILENAME}",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def tiktok_site_verification() -> PlainTextResponse:
    """Serve TikTok's URL-prefix verification file from the domain root.

    Returns the exact expected contents as plain text with no redirect so
    TikTok's verifier sees HTTP 200 at the root-level URL.
    """
    content = VERIFICATION_CONTENT
    try:
        if _VERIFICATION_FILE.is_file():
            content = _VERIFICATION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("Could not read TikTok verification file: %s", exc)
    return PlainTextResponse(content, media_type="text/plain")
