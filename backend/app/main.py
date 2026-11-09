import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .orchestrator import create_plan
from .providers import provider_is_configured
from .schemas import PlanRequest, PlanResponse

load_dotenv()

app = FastAPI(
    title="Autonomous AI Workforce API",
    version="0.1.0",
    description="Task planning and dynamic workforce orchestration foundation.",
)

origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "autonomous-ai-workforce-api"}


@app.get("/api/config/status")
async def config_status():
    return {
        "provider_configured": provider_is_configured(),
        "provider_type": "openai-compatible" if provider_is_configured() else None,
        "credentials_exposed": False,
    }


@app.post("/api/tasks/plan", response_model=PlanResponse)
async def plan_task(request: PlanRequest):
    return await create_plan(request)
