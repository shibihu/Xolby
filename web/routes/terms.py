import datetime
import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


@router.get("/terms", response_class=HTMLResponse)
async def terms_of_service(request: Request):
    contact_email = os.getenv("XOLBY_CONTACT_EMAIL", "XOLBY_CONTACT_EMAIL@example.com")
    return templates.TemplateResponse(
        request=request,
        name="terms.html",
        context={
            "active_page": "terms",
            "contact_email": contact_email,
            "current_year": datetime.datetime.now(datetime.timezone.utc).year,
        },
    )
