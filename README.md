# Folder-to-Deploy web app

A self-service page: a user uploads a zipped project folder, optionally types
in their own GitHub token and Render API key, and gets back a live deployed
URL. Works for Python apps (Django / FastAPI / Flask) — auto-detects the
framework and writes the right build/start commands.

## How it works
- `main.py` — FastAPI backend: serves the page, accepts the zip upload,
  runs the deploy pipeline in a background thread, exposes a status
  endpoint the page polls for live logs.
- `deploy_engine.py` — the actual pipeline: unzip → detect stack → create
  GitHub repo & push → create Render service → stream Render's build/runtime
  logs into the page while polling → return URL. On failure, the Render log
  lines show the actual error.
- `static/index.html` — the upload page (drag-and-drop zip, project name,
  optional token fields under "Use my own tokens").

## Deploy this app to Render (one-time, by you)
1. Push this folder to a GitHub repo (same way as any project — see the
   earlier `push-to-github.sh` script, or just `git init && git add . &&
   git commit && git push` manually).
2. In Render: New → Web Service → connect that repo. Render will read
   `render.yaml` automatically.
3. (Optional but recommended) In Render's dashboard, add environment
   variables `GITHUB_TOKEN` and `RENDER_API_KEY` set to *your* tokens.
   This makes the app work for visitors even if they leave the token
   fields blank — deploys will just happen under your accounts.
   If you skip this, every visitor MUST supply their own tokens.
4. Deploy. You'll get a URL like `https://deploy-app.onrender.com` —
   that's the page you share with users.

## Security notes (please read)
- Tokens typed into the page are sent over HTTPS to your server and used
  only in-memory for that one deploy job — they are not logged or stored
  to disk. Still, this app currently has **no auth, no rate limiting, and
  no per-user isolation** — anyone with the URL can trigger deploys.
  Before sharing this publicly, at minimum add:
  - a simple login/API-key gate in front of `/deploy`
  - a rate limit (e.g. per IP) so one visitor can't spam repo/service creation
  - a max upload size (Render's free plan has resource limits anyway)
- If you set server-side `GITHUB_TOKEN`/`RENDER_API_KEY` as defaults, any
  visitor who doesn't supply their own token will create repos and
  services **under your account**. Only do this for a trusted/internal tool,
  or add the auth gate above first.
- Only Python projects are auto-detected right now. Uploading a Node/static
  project will fall back to `python app.py` and fail — extending
  `deploy_engine.detect_stack` for other stacks is straightforward if you
  need that next.

## Local testing
```
pip install -r requirements.txt
uvicorn main:app --reload
```
Then open http://localhost:8000 — note that Render API calls will only
succeed once this is actually deployed on Render (or anywhere with outbound
internet access to api.render.com).
