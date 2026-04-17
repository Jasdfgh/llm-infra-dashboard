from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent import router as agent_router
from app.api.gaps import router as gaps_router
from app.api.projects import router as projects_router
from app.api.knowledge_api import router as knowledge_router
from app.api.review import router as review_router
from app.api.trends import router as trends_router
from app.models.database import init_db

app = FastAPI(title="LLM Infra Gap Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await init_db()


app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(gaps_router, prefix="/api/gaps", tags=["gaps"])
app.include_router(trends_router, prefix="/api/trends", tags=["trends"])
app.include_router(agent_router, prefix="/api/agent", tags=["agent"])
app.include_router(knowledge_router, prefix="/api/kb", tags=["knowledge"])
app.include_router(review_router, prefix="/api/review", tags=["review"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "name": "LLM Infra Gap Dashboard API"}
