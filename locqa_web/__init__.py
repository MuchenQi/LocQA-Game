"""locqa_web — the browser front end for locqa.

The checks live in `locqa` and know nothing about HTTP. This package adds what
a hosted tool needs around them: accounts, uploads, saved configs, a database
of past runs, and a job queue so a slow run never holds a request open.

    uvicorn locqa_web.app:app          # the web process
    python -m locqa_web.worker         # the worker process
"""
