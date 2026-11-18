"""HTTP layer: pages, forms, and a small JSON API.

Handlers do three kinds of work and no fourth: authenticate, validate input,
and read or write rows. Running locqa is never one of them — POST /runs
inserts a queued row and redirects, and the run page polls until a worker
has filled the result in. See jobs.py for why that is safe.
"""

# No `from __future__ import annotations` here, deliberately: FastAPI resolves
# string annotations against module globals, and the dependency aliases below
# (CurrentViewer) are local to create_app. Evaluated annotations just work.

import json
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from starlette.exceptions import HTTPException as StarletteHTTPException

from locqa import __version__ as LOCQA_VERSION
from locqa import registry

from . import jobs, limits, security
from .mail import Message, make_mailer
from .analysis import (
    ALLOWED_EXTENSIONS, TRANSLATION_EXTENSIONS, InputError, extension, safe_filename,
    validate_config,
)
from .db import init_db, make_engine, make_sessionmaker, utcnow
from .models import (
    DONE, FAILED, QUEUED, RUNNING, AuthSession, ConfigProfile, PasswordReset, Run, User,
)
from .settings import Settings

HERE = Path(__file__).resolve().parent
# The repo's samples/ directory, next to the package in a checkout. A
# non-editable install (the Docker image) points LOCQA_SAMPLES_DIR at it.
SAMPLES = Path(os.environ.get("LOCQA_SAMPLES_DIR") or HERE.parent / "samples")
COOKIE = "locqa_session"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SEVERITIES = ("Blocker", "Major", "Minor", "Info")
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

# Content-Security-Policy for the app's own pages: nothing inline, nothing
# from elsewhere, and only this origin may frame the report.
APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
# The proof sheet is a standalone document with inline CSS and JS. It is
# served sandboxed — an opaque origin with no access to the app's cookies or
# DOM — so even a rendering bug that let user text through as markup could
# not act as the logged-in user.
REPORT_CSP = (
    "sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
)


@dataclass(frozen=True)
class Viewer:
    user_id: int
    email: str
    csrf: str


class LoginRequired(Exception):
    pass


# -- samples -------------------------------------------------------------------

def _sample(name: str) -> bytes | None:
    path = SAMPLES / name
    return path.read_bytes() if path.is_file() else None


SAMPLE_CONFIG = (_sample("aetherbound.locqa.yml") or b"").decode("utf-8")


def _config_choices(Session, user_id: int) -> list[tuple[str, str]]:
    choices = [("default", "Defaults (no config)")]
    if SAMPLE_CONFIG:
        choices.append(("sample", "Sample project (aetherbound.locqa.yml)"))
    with Session() as s:
        for profile in s.scalars(
            select(ConfigProfile).where(ConfigProfile.user_id == user_id)
            .order_by(ConfigProfile.name)
        ):
            choices.append((f"profile:{profile.id}", profile.name))
    return choices


def _resolve_config(Session, user_id: int, choice: str) -> tuple[str, str]:
    """(display name, YAML snapshot) for a config choice from the run form."""
    if choice == "default":
        return "Defaults", ""
    if choice == "sample" and SAMPLE_CONFIG:
        return "Sample project", SAMPLE_CONFIG
    if choice.startswith("profile:"):
        try:
            profile_id = int(choice.split(":", 1)[1])
        except ValueError:
            raise InputError("Unknown config.") from None
        with Session() as s:
            profile = s.get(ConfigProfile, profile_id)
            if profile is None or profile.user_id != user_id:
                raise InputError("Unknown config.")
            return profile.name, profile.yaml_text
    raise InputError("Unknown config.")


# -- helpers -------------------------------------------------------------------

def _summary(run_row) -> dict:
    try:
        return json.loads(run_row.summary_json) if run_row.summary_json else {}
    except ValueError:
        return {}


def _read_upload(upload: UploadFile, limit: int, label: str) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise InputError(f"The {label} is larger than {limit // (1024 * 1024)} MB.")
    if not data:
        raise InputError(f"The {label} is empty.")
    return data


def _safe_next(value: str | None) -> str:
    """Only same-site relative paths, so ?next= cannot redirect off-site."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/runs"


def _fmt_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def _duration(run_row) -> str:
    if run_row.started_at and run_row.finished_at:
        seconds = (run_row.finished_at - run_row.started_at).total_seconds()
        return f"{seconds:.1f}s"
    return ""


# -- app factory ---------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)
    Session = make_sessionmaker(engine)

    # Set by every upload. The inline worker sleeps on it instead of polling
    # every second; without an inline worker nothing waits on it, and a
    # standalone worker hears about the run through Postgres NOTIFY instead.
    wakeup = jobs.Wakeup()
    mailer = make_mailer(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = threading.Event()
        thread = None
        if settings.inline_worker:
            thread = threading.Thread(
                target=jobs.work, args=(Session, settings, stop, wakeup),
                name="locqa-inline-worker", daemon=True,
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            wakeup.notify()
            if thread is not None:
                thread.join(timeout=settings.job_timeout_seconds + 5)

    app = FastAPI(
        title="locqa",
        version=LOCQA_VERSION,
        description="Localization QA for game scripts, EN → JA.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.Session = Session
    app.state.engine = engine
    app.state.wakeup = wakeup
    app.state.mailer = mailer

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["when"] = _fmt_time
    templates.env.globals["version"] = LOCQA_VERSION
    templates.env.globals["severities"] = SEVERITIES
    templates.env.globals["check_count"] = len(registry())
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def render(request: Request, name: str, viewer: Viewer | None = None,
               status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"viewer": viewer, **context}, status_code=status_code
        )

    # -- middleware --------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Reject oversized bodies before they are spooled to disk. A client
        # that omits Content-Length still meets the per-file limit in
        # _read_upload; this is the cheap first line.
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 3 * settings.max_upload_bytes:
            limit = f"{settings.max_upload_bytes / (1024 * 1024):g} MB"
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": f"Request too large (limit {limit} per file)."},
                                    status_code=413)
            return HTMLResponse(
                f"<!doctype html><meta charset=utf-8><title>Too large — locqa</title>"
                f"<link rel=stylesheet href=/static/app.css><main class='wrap narrow'>"
                f"<h1 class=page-title>That upload is too large.</h1>"
                f"<p>Files are limited to {limit}. <a href='/runs/new'>Back to the form</a></p>"
                f"</main>",
                status_code=413,
                headers={"Content-Security-Policy": APP_CSP},
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # FastAPI's /docs and /redoc pull their UI from a CDN, which APP_CSP
        # would block, leaving a blank page. They render no user data.
        if request.url.path not in DOCS_PATHS:
            response.headers.setdefault("Content-Security-Policy", APP_CSP)
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not signed in."}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/healthz", "/static/")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        return render(request, "error.html", viewer_of(request),
                      status_code=exc.status_code, code=exc.status_code, detail=exc.detail)

    # -- auth --------------------------------------------------------------

    def viewer_of(request: Request) -> Viewer | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        with Session() as s:
            row = s.execute(
                select(AuthSession, User)
                .join(User, User.id == AuthSession.user_id)
                .where(
                    AuthSession.token_hash == security.token_hash(token),
                    AuthSession.expires_at > utcnow(),
                )
            ).first()
            if row is None:
                return None
            auth, user = row
            return Viewer(user_id=user.id, email=user.email, csrf=auth.csrf_token)

    def client_ip(request: Request) -> str:
        # Behind the platform's proxy, uvicorn --proxy-headers has already
        # replaced this with the X-Forwarded-For address.
        return request.client.host if request.client else "unknown"

    def require_viewer(request: Request) -> Viewer:
        viewer = viewer_of(request)
        if viewer is None:
            raise LoginRequired()
        return viewer

    CurrentViewer = Annotated[Viewer, Depends(require_viewer)]

    def check_csrf(viewer: Viewer, token: str | None) -> None:
        # SameSite=Lax already stops cross-site form posts in current
        # browsers; the token is for the ones that do not honour it.
        if not token or not security.same(token, viewer.csrf):
            raise HTTPException(status_code=403, detail="Form expired. Reload and try again.")

    def start_session(user_id: int, response: Response) -> None:
        token = security.new_token()
        now = utcnow()
        with Session.begin() as s:
            s.execute(delete(AuthSession).where(
                AuthSession.user_id == user_id, AuthSession.expires_at <= now,
            ))
            s.add(AuthSession(
                token_hash=security.token_hash(token),
                csrf_token=security.new_token(),
                user_id=user_id,
                expires_at=now + timedelta(days=settings.session_days),
            ))
        response.set_cookie(
            COOKIE, token,
            max_age=settings.session_days * 86400,
            httponly=True, samesite="lax", secure=settings.secure_cookies, path="/",
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        viewer = viewer_of(request)
        if viewer:
            return RedirectResponse("/runs", status_code=303)
        return render(request, "index.html")

    @app.get("/register", response_class=HTMLResponse, include_in_schema=False)
    def register_form(request: Request):
        return render(request, "auth.html", mode="register", email="", error=None)

    @app.post("/register", include_in_schema=False)
    def register(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ):
        email = email.strip().lower()
        ip = client_ip(request)
        with Session.begin() as s:
            wait = limits.retry_after(s, "register", ip=ip)
            if wait is None:
                limits.record(s, "register", ip=ip)
        if wait is not None:
            return render(request, "auth.html", mode="register", email=email,
                          error=f"Too many new accounts from this network. "
                          f"Try again in {limits.describe(wait)}.", status_code=429)

        error = None
        if not _EMAIL.match(email) or len(email) > 320:
            error = "Enter a valid email address."
        elif len(password) < security.MIN_PASSWORD:
            error = f"Use at least {security.MIN_PASSWORD} characters for the password."
        if error:
            return render(request, "auth.html", mode="register", email=email,
                          error=error, status_code=400)
        try:
            with Session.begin() as s:
                user = User(email=email, password_hash=security.hash_password(password))
                s.add(user)
                s.flush()
                user_id = user.id
        except IntegrityError:
            return render(request, "auth.html", mode="register", email=email,
                          error="An account with that email already exists. Sign in instead.",
                          status_code=400)
        response = RedirectResponse("/runs/new", status_code=303)
        start_session(user_id, response)
        return response

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_form(request: Request, next: str | None = None):
        return render(request, "auth.html", mode="login", email="", error=None,
                      next=_safe_next(next))

    @app.post("/login", include_in_schema=False)
    def login(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str | None, Form()] = None,
    ):
        email = email.strip().lower()
        ip = client_ip(request)
        # Checked before the password is hashed: a locked-out guesser should
        # not even cost the server an scrypt.
        with Session() as s:
            wait = limits.retry_after(s, "login", email=email, ip=ip)
        if wait is not None:
            return render(request, "auth.html", mode="login", email=email,
                          error=f"Too many failed sign-ins. Try again in "
                          f"{limits.describe(wait)}, or reset your password.",
                          next=_safe_next(next), status_code=429)

        with Session() as s:
            user = s.scalars(select(User).where(User.email == email)).first()
            if user is None:
                security.verify_against_dummy(password)
                ok = False
            else:
                ok = security.verify_password(password, user.password_hash)
        with Session.begin() as s:
            if ok:
                limits.clear(s, "login", email=email)
            else:
                limits.record(s, "login", email=email, ip=ip)
        if not ok:
            return render(request, "auth.html", mode="login", email=email,
                          error="That email and password do not match an account.",
                          next=_safe_next(next), status_code=400)
        response = RedirectResponse(_safe_next(next), status_code=303)
        start_session(user.id, response)
        return response

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request, viewer: CurrentViewer,
               csrf: Annotated[str | None, Form()] = None):
        check_csrf(viewer, csrf)
        token = request.cookies.get(COOKIE, "")
        with Session.begin() as s:
            s.execute(delete(AuthSession).where(
                AuthSession.token_hash == security.token_hash(token)
            ))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    # -- password reset ----------------------------------------------------
    #
    # The request page answers identically whether or not the email has an
    # account, and the email goes out after the response, so neither the
    # page nor its timing says which emails are registered. Tokens are
    # random, stored hashed, single-use, and short-lived; using one signs the
    # account out everywhere, because a reset is often a response to someone
    # else having been signed in.

    def base_url(request: Request) -> str:
        return settings.base_url or str(request.base_url).rstrip("/")

    def live_reset(s, token: str) -> PasswordReset | None:
        return s.scalars(select(PasswordReset).where(
            PasswordReset.token_hash == security.token_hash(token),
            PasswordReset.used_at.is_(None),
            PasswordReset.expires_at > utcnow(),
        )).first()

    @app.get("/forgot", response_class=HTMLResponse, include_in_schema=False)
    def forgot_form(request: Request):
        return render(request, "forgot.html", sent=False, email="", error=None)

    @app.post("/forgot", include_in_schema=False)
    def forgot(request: Request, background: BackgroundTasks,
               email: Annotated[str, Form()]):
        email = email.strip().lower()
        ip = client_ip(request)
        with Session.begin() as s:
            wait = limits.retry_after(s, "reset", email=email, ip=ip)
            if wait is not None:
                return render(request, "forgot.html", sent=False, email=email,
                              error=f"Too many reset requests. Try again in "
                              f"{limits.describe(wait)}.", status_code=429)
            limits.record(s, "reset", email=email, ip=ip)

            user = s.scalars(select(User).where(User.email == email)).first()
            if user is not None:
                token = security.new_token()
                now = utcnow()
                # One live link at a time: a new request retires the old one.
                s.execute(delete(PasswordReset).where(PasswordReset.user_id == user.id))
                s.add(PasswordReset(
                    user_id=user.id, token_hash=security.token_hash(token),
                    created_at=now, expires_at=now + timedelta(minutes=settings.reset_minutes),
                ))
                link = f"{base_url(request)}/reset?token={token}"
                background.add_task(mailer.send, Message(
                    to=user.email,
                    subject="Reset your locqa password",
                    text=(
                        "Someone asked to reset the password for this locqa account.\n\n"
                        f"Set a new password here — the link works once, for "
                        f"{settings.reset_minutes} minutes:\n\n{link}\n\n"
                        "If it was not you, ignore this email; nothing has changed."
                    ),
                ))
        return render(request, "forgot.html", sent=True, email=email, error=None)

    @app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
    def reset_form(request: Request, token: str = ""):
        with Session() as s:
            valid = bool(token) and live_reset(s, token) is not None
        return render(request, "reset.html", token=token if valid else "",
                      valid=valid, error=None, status_code=200 if valid else 400)

    @app.post("/reset", include_in_schema=False)
    def reset(request: Request, token: Annotated[str, Form()],
              password: Annotated[str, Form()]):
        if len(password) < security.MIN_PASSWORD:
            return render(request, "reset.html", token=token, valid=True, status_code=400,
                          error=f"Use at least {security.MIN_PASSWORD} characters.")
        with Session.begin() as s:
            pending = live_reset(s, token)
            if pending is None:
                return render(request, "reset.html", token="", valid=False, error=None,
                              status_code=400)
            user_id = pending.user_id
            pending.used_at = utcnow()
            user = s.get(User, user_id)
            user.password_hash = security.hash_password(password)
            s.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            s.execute(delete(PasswordReset).where(
                PasswordReset.user_id == user_id, PasswordReset.id != pending.id,
            ))
            limits.clear(s, "login", email=user.email)
        response = RedirectResponse("/runs", status_code=303)
        start_session(user_id, response)
        return response

    # -- runs --------------------------------------------------------------

    def own_run(viewer: Viewer, run_id: int, *columns):
        """A run the viewer owns, or 404. Someone else's run is also 404, not
        403: whether a run id exists is itself not the viewer's business."""
        with Session() as s:
            query = select(*columns) if columns else select(Run)
            row = s.execute(
                query.where(Run.id == run_id, Run.user_id == viewer.user_id)
            ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="No such run.")
        return row if columns else row[0]

    LIST_COLUMNS = (
        Run.id, Run.filename, Run.translation_filename, Run.config_name, Run.status,
        Run.created_at, Run.started_at, Run.finished_at, Run.summary_json, Run.error,
    )

    @app.get("/runs", response_class=HTMLResponse, include_in_schema=False)
    def runs_page(request: Request, viewer: CurrentViewer):
        with Session() as s:
            rows = s.execute(
                select(*LIST_COLUMNS).where(Run.user_id == viewer.user_id)
                .order_by(Run.created_at.desc(), Run.id.desc()).limit(200)
            ).all()
        runs = [{"row": r, "summary": _summary(r), "duration": _duration(r)} for r in rows]
        pending = any(r.status in (QUEUED, RUNNING) for r in rows)
        return render(request, "runs.html", viewer, runs=runs, pending=pending)

    @app.get("/runs/new", response_class=HTMLResponse, include_in_schema=False)
    def new_run_form(request: Request, viewer: CurrentViewer):
        return render(
            request, "new_run.html", viewer,
            configs=_config_choices(Session, viewer.user_id), error=None,
            extensions=sorted(ALLOWED_EXTENSIONS), has_samples=bool(SAMPLE_CONFIG),
            max_mb=settings.max_upload_bytes // (1024 * 1024),
        )

    def enqueue(viewer: Viewer, **fields) -> int:
        with Session.begin() as s:
            run = Run(user_id=viewer.user_id, status=QUEUED, **fields)
            s.add(run)
            s.flush()
            run_id = run.id
            jobs.notify_new_run(s, run_id)      # NOTIFY, delivered at commit
        wakeup.notify()                         # the inline worker, if any
        return run_id

    @app.post("/runs", include_in_schema=False)
    def create_run(
        request: Request,
        viewer: CurrentViewer,
        file: Annotated[UploadFile, File()],
        config: Annotated[str, Form()] = "default",
        translation: Annotated[UploadFile | None, File()] = None,
        csrf: Annotated[str | None, Form()] = None,
    ):
        check_csrf(viewer, csrf)
        try:
            filename = safe_filename(file.filename or "")
            ext = extension(filename)
            if ext not in ALLOWED_EXTENSIONS:
                raise InputError(
                    f"locqa cannot read {ext or 'files without an extension'}. "
                    f"Upload one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
                )
            data = _read_upload(file, settings.max_upload_bytes, "file")

            t_name = t_data = None
            if translation is not None and translation.filename:
                if ext not in TRANSLATION_EXTENSIONS:
                    raise InputError("A separate translation file only applies to Ren'Py scripts.")
                t_name = safe_filename(translation.filename)
                if extension(t_name) != ".rpy":
                    raise InputError("The translation file must be a .rpy file.")
                t_data = _read_upload(translation, settings.max_upload_bytes, "translation file")

            config_name, config_yaml = _resolve_config(Session, viewer.user_id, config)
            # Validated now so a bad config is reported on the form, not as a
            # failed run a minute later.
            validate_config(config_yaml, config_name)
        except InputError as exc:
            return render(
                request, "new_run.html", viewer, status_code=400,
                configs=_config_choices(Session, viewer.user_id), error=str(exc),
                extensions=sorted(ALLOWED_EXTENSIONS), has_samples=bool(SAMPLE_CONFIG),
                max_mb=settings.max_upload_bytes // (1024 * 1024),
            )

        run_id = enqueue(
            viewer, filename=filename, file_bytes=data,
            translation_filename=t_name, translation_bytes=t_data,
            config_name=config_name, config_yaml=config_yaml,
        )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/sample", include_in_schema=False)
    def create_sample_run(viewer: CurrentViewer,
                          which: Annotated[str, Form()] = "dialogue",
                          csrf: Annotated[str | None, Form()] = None):
        check_csrf(viewer, csrf)
        if which == "sheet":
            files = ("aetherbound_ui.csv", None)
        else:
            files = ("aetherbound.rpy", "aetherbound_ja.rpy")
        data = _sample(files[0])
        if data is None or not SAMPLE_CONFIG:
            raise HTTPException(status_code=404, detail="Samples are not installed.")
        run_id = enqueue(
            viewer, filename=files[0], file_bytes=data,
            translation_filename=files[1],
            translation_bytes=_sample(files[1]) if files[1] else None,
            config_name="Sample project", config_yaml=SAMPLE_CONFIG,
        )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    def run_page(request: Request, run_id: int, viewer: CurrentViewer):
        row = own_run(viewer, run_id, *LIST_COLUMNS, Run.attempts)
        return render(request, "run.html", viewer, run=row, summary=_summary(row),
                      duration=_duration(row))

    @app.get("/runs/{run_id}/report", include_in_schema=False)
    def run_report(run_id: int, viewer: CurrentViewer, download: bool = False):
        row = own_run(viewer, run_id, Run.report_html, Run.filename)
        if not row.report_html:
            raise HTTPException(status_code=404, detail="This run has no report yet.")
        headers = {"Content-Security-Policy": REPORT_CSP}
        if download:
            stem = row.filename.rsplit(".", 1)[0]
            headers["Content-Disposition"] = f'attachment; filename="{stem}-locqa.html"'
        return HTMLResponse(row.report_html, headers=headers)

    @app.get("/runs/{run_id}/findings.json", include_in_schema=False)
    def run_findings(run_id: int, viewer: CurrentViewer):
        row = own_run(viewer, run_id, Run.findings_json, Run.summary_json, Run.filename,
                      Run.status)
        if row.status != DONE:
            raise HTTPException(status_code=404, detail="This run has no findings yet.")
        stem = row.filename.rsplit(".", 1)[0]
        body = json.dumps({
            "summary": json.loads(row.summary_json or "{}"),
            "findings": json.loads(row.findings_json or "[]"),
        }, ensure_ascii=False, indent=2)
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{stem}-locqa.json"',
        })

    @app.post("/runs/{run_id}/retry", include_in_schema=False)
    def retry_run(run_id: int, viewer: CurrentViewer,
                  csrf: Annotated[str | None, Form()] = None):
        check_csrf(viewer, csrf)
        own_run(viewer, run_id, Run.id)
        with Session.begin() as s:
            s.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == FAILED)
                .values(status=QUEUED, attempts=0, error=None, started_at=None,
                        finished_at=None, locked_until=None, created_at=utcnow())
            )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/delete", include_in_schema=False)
    def delete_run(run_id: int, viewer: CurrentViewer,
                   csrf: Annotated[str | None, Form()] = None):
        check_csrf(viewer, csrf)
        own_run(viewer, run_id, Run.id)
        with Session.begin() as s:
            s.execute(delete(Run).where(Run.id == run_id, Run.user_id == viewer.user_id))
        return RedirectResponse("/runs", status_code=303)

    # -- configs -----------------------------------------------------------

    def configs_context(viewer: Viewer, edit_id: int | None):
        with Session() as s:
            profiles = list(s.scalars(
                select(ConfigProfile).where(ConfigProfile.user_id == viewer.user_id)
                .order_by(ConfigProfile.name)
            ))
        editing = next((p for p in profiles if p.id == edit_id), None)
        return {
            "profiles": profiles,
            "form_name": editing.name if editing else "",
            "form_yaml": editing.yaml_text if editing else SAMPLE_CONFIG,
        }

    @app.get("/configs", response_class=HTMLResponse, include_in_schema=False)
    def configs_page(request: Request, viewer: CurrentViewer, edit: int | None = None):
        return render(request, "configs.html", viewer, error=None, saved=None,
                      **configs_context(viewer, edit))

    @app.post("/configs", include_in_schema=False)
    def save_config(
        request: Request,
        viewer: CurrentViewer,
        name: Annotated[str, Form()],
        yaml_text: Annotated[str, Form()] = "",
        csrf: Annotated[str | None, Form()] = None,
    ):
        check_csrf(viewer, csrf)
        name = name.strip()
        context = configs_context(viewer, None)
        context.update(form_name=name, form_yaml=yaml_text)
        try:
            if not name or len(name) > 80:
                raise InputError("Give the config a name of 1–80 characters.")
            if name.lower() in ("defaults", "sample project"):
                raise InputError(f"“{name}” is reserved for a built-in config.")
            validate_config(yaml_text, name)
        except InputError as exc:
            return render(request, "configs.html", viewer, error=str(exc), saved=None,
                          status_code=400, **context)

        with Session.begin() as s:
            existing = s.scalars(select(ConfigProfile).where(
                ConfigProfile.user_id == viewer.user_id, ConfigProfile.name == name,
            )).first()
            if existing:
                existing.yaml_text = yaml_text
                existing.updated_at = utcnow()
            else:
                s.add(ConfigProfile(user_id=viewer.user_id, name=name, yaml_text=yaml_text))
        context = configs_context(viewer, None)
        return render(request, "configs.html", viewer, error=None, saved=name, **context)

    @app.post("/configs/{config_id}/delete", include_in_schema=False)
    def delete_config(config_id: int, viewer: CurrentViewer,
                      csrf: Annotated[str | None, Form()] = None):
        check_csrf(viewer, csrf)
        with Session.begin() as s:
            s.execute(delete(ConfigProfile).where(
                ConfigProfile.id == config_id, ConfigProfile.user_id == viewer.user_id,
            ))
        return RedirectResponse("/configs", status_code=303)

    # -- JSON API ----------------------------------------------------------

    def run_json(row) -> dict:
        summary = _summary(row)
        return {
            "id": row.id,
            "status": row.status,
            "file": row.filename,
            "translation": row.translation_filename,
            "config": row.config_name,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
            "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
            "error": row.error,
            "summary": summary or None,
            "links": {
                "page": f"/runs/{row.id}",
                "report": f"/runs/{row.id}/report" if row.status == DONE else None,
                "findings": f"/runs/{row.id}/findings.json" if row.status == DONE else None,
            },
        }

    @app.get("/api/runs", tags=["runs"])
    def api_runs(viewer: CurrentViewer, limit: int = 50) -> list[dict]:
        """The signed-in user's runs, newest first."""
        with Session() as s:
            rows = s.execute(
                select(*LIST_COLUMNS).where(Run.user_id == viewer.user_id)
                .order_by(Run.created_at.desc(), Run.id.desc()).limit(max(1, min(limit, 200)))
            ).all()
        return [run_json(r) for r in rows]

    @app.get("/api/runs/{run_id}", tags=["runs"])
    def api_run(run_id: int, viewer: CurrentViewer) -> dict:
        """One run's status and summary. The run page polls this."""
        return run_json(own_run(viewer, run_id, *LIST_COLUMNS))

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        with Session() as s:
            s.execute(text("SELECT 1"))
            queued = s.scalar(
                select(Run.id).where(Run.status == QUEUED).order_by(Run.created_at).limit(1)
            )
        return {"ok": True, "locqa": LOCQA_VERSION, "queue_idle": queued is None}

    return app


def app_from_env() -> FastAPI:
    return create_app()


# `uvicorn locqa_web.app:app` — built lazily so importing the module (in tests,
# or the worker) does not open a database.
def __getattr__(name: str):
    if name == "app":
        globals()["app"] = application = create_app()
        return application
    raise AttributeError(name)
