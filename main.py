import os
import uuid
import threading
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from deploy_engine import run_pipeline, sanitize_repo_name, github_repo_exists, find_render_service

load_dotenv()

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

JOBS = {}

# Optional server-wide fallback tokens from .env / environment (used only
# when a visitor doesn't type in their own tokens on the form).
DEFAULT_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DEFAULT_RENDER_TOKEN = os.environ.get("RENDER_API_KEY")


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/deploy")
async def deploy(
    file: UploadFile = File(...),
    project_name: str = Form(...),
    github_token: str = Form(""),
    render_token: str = Form(""),
    overwrite: bool = Form(False),
    build_command: str = Form(""),
    start_command: str = Form(""),
):
    gh_token = github_token.strip() or DEFAULT_GITHUB_TOKEN
    rd_token = render_token.strip() or DEFAULT_RENDER_TOKEN

    if not gh_token or not rd_token:
        return JSONResponse(
            {"error": "Missing GitHub token or Render token, and no server default is configured."},
            status_code=400,
        )

    repo_name = sanitize_repo_name(project_name)
    if not overwrite:
        try:
            repo_exists = github_repo_exists(gh_token, repo_name)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        service_exists = find_render_service(rd_token, repo_name) is not None
        if repo_exists or service_exists:
            return JSONResponse(
                {
                    "conflict": True,
                    "name": repo_name,
                    "repo_exists": repo_exists,
                    "service_exists": service_exists,
                },
                status_code=409,
            )

    job_id = str(uuid.uuid4())
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    with os.fdopen(tmp_fd, "wb") as f:
        f.write(await file.read())

    job = {"status": "queued", "logs": [], "render_logs": [], "repo_url": None, "live_url": None}
    JOBS[job_id] = job

    thread = threading.Thread(
        target=run_pipeline,
        args=(job, tmp_path, project_name, gh_token, rd_token, overwrite, build_command, start_command),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job id"}, status_code=404)
    return job
