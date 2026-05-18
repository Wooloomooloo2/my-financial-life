# ===========================================================================
# main.py
#
# Entry point for My Financial Life.
#
# Starts the FastAPI application, loads the ontology on startup, and
# opens the app in the default browser.
#
# Run with:
#   python main.py
# ===========================================================================

import logging
import threading
import time
import webbrowser

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.data.store import store
from app.data.ontology_loader import load_ontology

# ---------------------------------------------------------------------------
# Logging — show INFO and above in the console with a clean format
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown events
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("My Financial Life starting up...")
    load_ontology(store)
    logger.info("Startup complete.")
    yield
    # Shutdown — nothing to close explicitly; Oxigraph flushes on process exit


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="My Financial Life",
    description="Personal finance tracking — private, local, and powerful.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",   # Swagger UI available at /api/docs during development
    redoc_url=None,
)

# Serve static files (CSS, JS) from /static
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Root route — placeholder dashboard
# Replaced with a real Jinja2 template in the next phase.
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>My Financial Life</title>
        <link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.10/dist/full.min.css" rel="stylesheet">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-base-200 min-h-screen flex items-center justify-center">
        <div class="card bg-base-100 shadow-xl w-full max-w-md">
            <div class="card-body items-center text-center gap-4">
                <h1 class="card-title text-3xl font-bold">My Financial Life</h1>
                <p class="text-base-content/70">
                    Application is running.<br>
                    Ontology loaded successfully.
                </p>
                <div class="badge badge-success badge-lg">v0.1.0</div>
                <div class="text-sm text-base-content/50 mt-2">
                    Dashboard coming soon.
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Browser opener — waits 1.5 seconds then opens the app in the default
# browser. The delay gives uvicorn time to start before the browser loads.
# ---------------------------------------------------------------------------

def _open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )