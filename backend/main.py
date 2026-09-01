"""FastAPI 应用工厂。一期为只读服务，所有路由挂 /api/v1。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.applications import router as applications_router
from backend.api.episodes import router as episodes_router
from backend.api.experiments import router as experiments_router
from backend.api.harnesses import router as harnesses_router
from backend.api.infrastructure import router as infrastructure_router
from backend.api.mcp_tools import router as mcp_tools_router
from backend.api.meta import router as meta_router
from backend.api.models import router as models_router
from backend.api.observability import router as observability_router

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    app = FastAPI(title="Benchmark Frontend API", docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(meta_router, prefix=API_PREFIX)
    app.include_router(infrastructure_router, prefix=API_PREFIX)
    app.include_router(experiments_router, prefix=API_PREFIX)
    app.include_router(applications_router, prefix=API_PREFIX)
    app.include_router(models_router, prefix=API_PREFIX)
    app.include_router(harnesses_router, prefix=API_PREFIX)
    app.include_router(episodes_router, prefix=API_PREFIX)
    app.include_router(mcp_tools_router, prefix=API_PREFIX)
    app.include_router(observability_router, prefix=API_PREFIX)
    return app


app = create_app()
