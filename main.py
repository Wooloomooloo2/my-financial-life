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
from fastapi.templating import Jinja2Templates

from app.data.store import store
from app.data.ontology_loader import load_ontology
from app.core.templates import templates
from app.api.settings import router as settings_router
from app.api.accounts import router as accounts_router
from app.api.transactions import router as transactions_router
from app.api.transactions import router as transactions_router

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

# Routers
app.include_router(settings_router)
app.include_router(accounts_router)
app.include_router(transactions_router)


# ---------------------------------------------------------------------------
# Root route
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
        <link href="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css" rel="stylesheet">
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-base-200 min-h-screen flex items-center justify-center">
        <div class="card bg-base-100 shadow-xl w-full max-w-md">
            <div class="card-body items-center text-center gap-4">
                <h1 class="card-title text-3xl font-bold">My Financial Life</h1>
                <p class="text-base-content/70">Application is running.</p>
                <div class="badge badge-success badge-lg">v0.1.0</div>
                <a href="/accounts" class="btn btn-primary btn-sm mt-2">Go to accounts</a>
            </div>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Ontology debug endpoints (development only)
# ---------------------------------------------------------------------------

@app.get("/ontology/status")
async def ontology_status():
    """Shows ontology triple count — confirms both TTL files are loaded."""
    from app.core.ontology.namespaces import ONTOLOGY_GRAPH
    count = sum(1 for _ in store.quads_for_pattern(None, None, None, ONTOLOGY_GRAPH))
    return {
        "ontology_triple_count": count,
        "status": "ok" if count > 200 else "low — mfl-ontology.ttl may not be loaded",
    }


@app.get("/ontology/reload")
async def ontology_reload():
    """Force reloads both ontology TTL files. Use when TTL files change."""
    from app.data.ontology_loader import load_ontology
    from app.core.ontology.namespaces import ONTOLOGY_GRAPH
    load_ontology(store, force=True)
    count = sum(1 for _ in store.quads_for_pattern(None, None, None, ONTOLOGY_GRAPH))
    return {"reloaded": True, "ontology_triple_count": count}


@app.get("/ontology/categories-debug")
async def categories_debug():
    """Debug — dumps raw SPARQL results for transaction categories."""
    from app.core.ontology.namespaces import ONTOLOGY_GRAPH, MFLX
    scheme = MFLX + "TransactionCategoryScheme"
    income = MFLX + "TransactionCategory_Income"
    sparql = f"""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?concept ?label ?broader
        WHERE {{
            GRAPH <{ONTOLOGY_GRAPH.value}> {{
                ?concept skos:inScheme <{scheme}> ;
                         skos:prefLabel ?label .
                OPTIONAL {{ ?concept skos:broader ?broader }}
            }}
        }}
        LIMIT 40
    """
    results = []
    for row in store.query(sparql):
        results.append({
            "concept": row["concept"].value,
            "label":   row["label"].value,
            "broader": row["broader"].value if row["broader"] else None,
        })
    return {
        "scheme_iri":  scheme,
        "income_top":  income,
        "result_count": len(results),
        "results":     results,
    }


@app.get("/ontology/parse-mfl")
async def parse_mfl():
    """
    Diagnostic — parses mfl-ontology.ttl in complete isolation.
    Tests the file, its size, and how many triples pyoxigraph extracts from it.
    If triple count is 0, the file has a parsing issue on this machine.
    If triple count is > 0, the issue is with loading into the persistent store.
    """
    from pathlib import Path
    from pyoxigraph import Store

    path = Path("docs/ontology/mfl-ontology.ttl")
    if not path.exists():
        return {"error": "file not found", "searched_path": str(path.resolve())}

    size = path.stat().st_size

    temp = Store()
    with open(path, "rb") as f:
        temp.load(f, format="text/turtle")

    count = sum(1 for _ in temp.quads_for_pattern(None, None, None, None))

    sample_subjects = []
    for i, quad in enumerate(temp.quads_for_pattern(None, None, None, None)):
        sample_subjects.append(quad.subject.value)
        if i >= 4:
            break

    return {
        "file_size_bytes": size,
        "parsed_triple_count": count,
        "sample_subjects": sample_subjects,
    }
    """Debug — scans for skos:inScheme triples to find what schemes are loaded."""
    from app.core.ontology.namespaces import ONTOLOGY_GRAPH
    from pyoxigraph import NamedNode
    skos_inscheme = NamedNode("http://www.w3.org/2004/02/skos/core#inScheme")
    results = []
    for quad in store.quads_for_pattern(None, skos_inscheme, None, ONTOLOGY_GRAPH):
        results.append({
            "subject": quad.subject.value,
            "scheme":  quad.object.value,
        })
    schemes = list({r["scheme"] for r in results})
    return {
        "inscheme_triple_count": len(results),
        "distinct_schemes": schemes,
        "sample_subjects": [r["subject"] for r in results[:5]],
    }


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