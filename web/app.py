import logging
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.routes import home, privacy, terms, tiktok_oauth, tiktok_verification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("web")

app = FastAPI(
    title="Xolby Web Backend",
    description="Official Xolby Website, Legal Pages, and TikTok OAuth Callback Backend",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory="web/static"), name="static")

app.include_router(home.router)
app.include_router(privacy.router)
app.include_router(terms.router)
app.include_router(tiktok_oauth.router)
app.include_router(tiktok_verification.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event():
    diag = tiktok_oauth.get_config_diagnostics()
    log.info("[WEB SERVER] Starting Xolby Web Backend")
    log.info("[WEB SERVER] Config Diagnostics: %s", diag)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("web.app:app", host="0.0.0.0", port=port, reload=True)
