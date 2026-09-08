import os
import re
import subprocess
import time
import zipfile
import shutil
import tempfile
import requests

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"


def log(job, msg):
    job["logs"].append(msg)
    print(msg)


def extract_zip(zip_path, dest_dir):
    dest_dir = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in z.namelist():
            target = os.path.abspath(os.path.join(dest_dir, member))
            if target != dest_dir and not target.startswith(dest_dir + os.sep):
                raise RuntimeError(f"Unsafe path in zip archive: {member}")
        z.extractall(dest_dir)
    # If the zip contained a single top-level folder, flatten it
    entries = [e for e in os.listdir(dest_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest_dir, entries[0])):
        inner = os.path.join(dest_dir, entries[0])
        for item in os.listdir(inner):
            shutil.move(os.path.join(inner, item), os.path.join(dest_dir, item))
        shutil.rmtree(inner)
    return dest_dir


def _find_web_object_name(text, framework):
    patterns = {
        "fastapi": r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*FastAPI\s*\(",
        "flask": r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Flask\s*\(",
    }
    match = re.search(patterns[framework], text)
    return match.group(1) if match else None


def detect_stack(folder, job):
    build_command = "pip install -r requirements.txt"
    start_command = None
    env = "python"

    files = []
    skip_dirs = {".git", "venv", ".venv", "env", ".env", "node_modules", "__pycache__"}
    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(root, f))

    if os.path.exists(os.path.join(folder, "manage.py")):
        wsgi_dirs = [
            os.path.relpath(os.path.dirname(p), folder)
            for p in files
            if os.path.basename(p) == "wsgi.py"
        ]
        module = wsgi_dirs[0].replace(os.sep, ".") if wsgi_dirs else "app"
        start_command = f"gunicorn {module}.wsgi:application --bind 0.0.0.0:$PORT"
        build_command = "pip install -r requirements.txt && python manage.py collectstatic --noinput || true"
        log(job, "Detected Django project")
    else:
        entry = None
        fastapi_candidates = []
        flask_candidates = []
        for p in files:
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            obj_name = _find_web_object_name(text, "fastapi")
            if obj_name:
                fastapi_candidates.append((p, obj_name))
            obj_name = _find_web_object_name(text, "flask")
            if obj_name:
                flask_candidates.append((p, obj_name))

        for p, obj_name in sorted(fastapi_candidates, key=lambda x: (os.path.basename(x[0]) not in {"main.py", "app.py", "application.py"}, x[0])):
            entry = p
            mod = os.path.splitext(os.path.relpath(p, folder))[0].replace(os.sep, ".")
            start_command = f"uvicorn {mod}:{obj_name} --host 0.0.0.0 --port $PORT"
            log(job, f"Detected FastAPI app ({os.path.relpath(p, folder)})")
            break

        if not entry:
            for p, obj_name in sorted(flask_candidates, key=lambda x: (os.path.basename(x[0]) not in {"main.py", "app.py", "application.py"}, x[0])):
                entry = p
                mod = os.path.splitext(os.path.relpath(p, folder))[0].replace(os.sep, ".")
                start_command = f"gunicorn {mod}:{obj_name} --bind 0.0.0.0:$PORT"
                log(job, f"Detected Flask app ({os.path.relpath(p, folder)})")
                break

        if not entry:
            start_command = "python app.py"
            log(job, "Could not auto-detect framework, defaulting to 'python app.py'")

    return build_command, start_command


def finalize_project(folder, service_name, build_command, start_command):
    req_path = os.path.join(folder, "requirements.txt")
    req_lines = []
    if os.path.exists(req_path):
        req_lines = open(req_path).read().splitlines()
    if "gunicorn" in start_command and not any(l.lower().startswith("gunicorn") for l in req_lines):
        req_lines.append("gunicorn")
    if "uvicorn" in start_command and not any(l.lower().startswith("uvicorn") for l in req_lines):
        req_lines.append("uvicorn")
    with open(req_path, "w") as f:
        f.write("\n".join(req_lines) + "\n")

    # Render re-applies render.yaml from the repo on every deploy, so it must
    # carry the real service name and the final commands.
    render_yaml = f"""services:
  - type: web
    name: {service_name}
    runtime: python
    plan: free
    buildCommand: {build_command}
    startCommand: {start_command}
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
"""
    with open(os.path.join(folder, "render.yaml"), "w") as f:
        f.write(render_yaml)


def run(cmd, cwd=None, secret=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        scrub = (lambda s: s.replace(secret, "***")) if secret else (lambda s: s)
        raise RuntimeError(
            f"Command failed: {scrub(cmd)}\n{scrub(result.stdout)}\n{scrub(result.stderr)}"
        )
    return result.stdout


def sanitize_repo_name(project_name):
    return re.sub(r"[^a-zA-Z0-9-]", "-", project_name.lower()).strip("-") or "my-app"


def github_repo_exists(github_token, repo_name):
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github+json"}
    me = requests.get(f"{GITHUB_API}/user", headers=headers)
    if me.status_code != 200:
        raise RuntimeError(f"GitHub token invalid: {me.text}")
    username = me.json()["login"]
    resp = requests.get(f"{GITHUB_API}/repos/{username}/{repo_name}", headers=headers)
    return resp.status_code == 200


def find_render_service(render_token, service_name):
    headers = {"Authorization": f"Bearer {render_token}"}
    resp = requests.get(
        f"{RENDER_API}/services", headers=headers,
        params={"name": service_name, "limit": 50},
    )
    if resp.status_code != 200:
        return None
    for item in resp.json():
        svc = item.get("service", {})
        if svc.get("name") == service_name:
            return svc
    return None


def push_to_github(folder, repo_name, github_token, job):
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github+json"}
    me = requests.get(f"{GITHUB_API}/user", headers=headers)
    if me.status_code != 200:
        raise RuntimeError(f"GitHub token invalid: {me.text}")
    username = me.json()["login"]

    log(job, f"Creating GitHub repo '{repo_name}' under {username}...")
    resp = requests.post(
        f"{GITHUB_API}/user/repos",
        headers=headers,
        json={"name": repo_name, "private": False, "auto_init": False},
    )
    repo_exists = resp.status_code == 422
    if repo_exists:
        log(job, "Repo already exists, merging into it.")
    elif resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create repo: {resp.text}")

    repo_url = f"https://github.com/{username}/{repo_name}"
    auth_url = f"https://{github_token}@github.com/{username}/{repo_name}.git"

    if repo_exists:
        # Clone the existing repo, overlay the uploaded files on top (new
        # files win per-file, files absent from the upload are kept), then
        # commit and push only if anything actually changed.
        base = os.path.join(folder, ".repo_base")
        os.makedirs(base)
        run(f"git clone -q {auth_url} .", cwd=base, secret=github_token)
        if run("git rev-parse --verify -q origin/main", cwd=base, check=False).strip():
            run("git checkout -q -B main origin/main", cwd=base)
        else:
            run("git checkout -q -B main", cwd=base)
        for item in os.listdir(folder):
            if item in (".git", ".repo_base"):
                continue
            s = os.path.join(folder, item)
            d = os.path.join(base, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        run("git add -A", cwd=base)
        if not run("git status --porcelain", cwd=base).strip():
            log(job, "No file changes detected — keeping the repo as-is, redeploying current code.")
            return repo_url, username, False
        run('git -c user.email="deploy@app" -c user.name="deploy-bot" commit -q -m "Update via deploy app"', cwd=base)
        run("git push origin main", cwd=base, secret=github_token)
        log(job, f"Merged and pushed updates to {repo_url}")
        return repo_url, username, True

    run("git init -q", cwd=folder)
    run("git add .", cwd=folder)
    run('git -c user.email="deploy@app" -c user.name="deploy-bot" commit -q -m "Initial commit" --allow-empty', cwd=folder)
    run("git branch -M main", cwd=folder)
    run("git remote remove origin", cwd=folder, check=False)
    run(f"git remote add origin {auth_url}", cwd=folder, secret=github_token)
    run("git push -u origin main -f", cwd=folder, secret=github_token)

    log(job, f"Pushed to {repo_url}")
    return repo_url, username, True


def deploy_to_render(repo_url, service_name, render_token, build_command, start_command, job, overwrite=False):
    headers = {"Authorization": f"Bearer {render_token}", "Content-Type": "application/json"}

    existing = find_render_service(render_token, service_name) if overwrite else None
    if existing:
        service_id = existing["id"]
        log(job, f"Render service '{service_name}' already exists, updating it...")
        patch = requests.patch(
            f"{RENDER_API}/services/{service_id}",
            headers=headers,
            json={"serviceDetails": {
                "env": "python",
                "envSpecificDetails": {
                    "buildCommand": build_command,
                    "startCommand": start_command,
                },
            }},
        )
        if patch.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to update Render service: {patch.text}")
        resp = requests.post(f"{RENDER_API}/services/{service_id}/deploys", headers=headers)
        if resp.status_code not in (200, 201, 202):
            # The git push may have already auto-triggered a deploy (autoDeploy: yes).
            # If one is running, just watch it instead of failing.
            latest = requests.get(f"{RENDER_API}/services/{service_id}/deploys?limit=1", headers=headers)
            active = {"created", "build_in_progress", "update_in_progress", "pre_deploy_in_progress"}
            ongoing = (
                latest.status_code == 200
                and latest.json()
                and latest.json()[0]["deploy"]["status"] in active
            )
            if ongoing:
                log(job, "A deploy is already running (auto-triggered by the push), watching it...")
            else:
                raise RuntimeError(f"Failed to trigger Render deploy: {resp.status_code} {resp.text}")
    else:
        owners_resp = requests.get(f"{RENDER_API}/owners", headers=headers)
        if owners_resp.status_code != 200:
            raise RuntimeError(f"Render token invalid: {owners_resp.text}")
        owner_id = owners_resp.json()[0]["owner"]["id"]

        log(job, f"Creating Render service '{service_name}'...")
        body = {
            "type": "web_service",
            "name": service_name,
            "ownerId": owner_id,
            "repo": repo_url,
            "branch": "main",
            "autoDeploy": "yes",
            "serviceDetails": {
                "env": "python",
                "plan": "free",
                "region": "oregon",
                "envSpecificDetails": {
                    "buildCommand": build_command,
                    "startCommand": start_command,
                },
            },
        }
        resp = requests.post(f"{RENDER_API}/services", headers=headers, json=body)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to create Render service: {resp.text}")
        service = resp.json()["service"]
        service_id = service["id"]

    log(job, "Waiting for build to go live (this can take a few minutes)...")
    for i in range(60):
        deploys = requests.get(f"{RENDER_API}/services/{service_id}/deploys?limit=1", headers=headers)
        deploy_list = deploys.json()
        if not deploy_list:
            log(job, "  waiting for first deploy to be created...")
            time.sleep(10)
            continue
        status = deploy_list[0]["deploy"]["status"]
        log(job, f"  deploy status: {status}")
        if status == "live":
            break
        if status in ("build_failed", "update_failed", "canceled"):
            raise RuntimeError(f"Render deploy failed with status: {status}")
        time.sleep(10)
    else:
        raise RuntimeError("Timed out waiting for deploy to go live")

    svc = requests.get(f"{RENDER_API}/services/{service_id}", headers=headers).json()
    service = svc.get("service", svc) if isinstance(svc, dict) else {}
    details = service.get("serviceDetails", {}) if isinstance(service, dict) else {}
    url = details.get("url") if isinstance(details, dict) else None
    if not url:
        url = service.get("url") if isinstance(service, dict) else None
    if not url:
        raise RuntimeError(f"Could not determine Render service URL from response: {svc}")
    return url


def run_pipeline(job, zip_path, project_name, github_token, render_token, overwrite=False,
                 build_override="", start_override=""):
    try:
        job["status"] = "running"
        workdir = tempfile.mkdtemp()
        log(job, "Extracting uploaded folder...")
        extract_zip(zip_path, workdir)

        log(job, "Detecting stack...")
        build_command, start_command = detect_stack(workdir, job)

        if build_override.strip():
            build_command = build_override.strip()
            log(job, f"Using custom build command: {build_command}")
        if start_override.strip():
            start_command = start_override.strip()
            log(job, f"Using custom start command: {start_command}")

        repo_name = sanitize_repo_name(project_name)
        finalize_project(workdir, repo_name, build_command, start_command)
        repo_url, username, pushed = push_to_github(workdir, repo_name, github_token, job)

        live_url = deploy_to_render(repo_url, repo_name, render_token, build_command, start_command, job, overwrite=overwrite)

        job["status"] = "done"
        job["repo_url"] = repo_url
        job["live_url"] = live_url
        log(job, f"Deployed: {live_url}")
    except Exception as e:
        job["status"] = "error"
        log(job, f"ERROR: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            os.remove(zip_path)
        except Exception:
            pass
