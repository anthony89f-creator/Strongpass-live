# Gunicorn configuration for Strongman Competition Engine
# Run: gunicorn -c gunicorn.conf.py server:app

bind        = "127.0.0.1:8080"
workers     = 1          # MUST be 1 for SQLite + SSE (no shared memory between processes)
threads     = 8          # Handle concurrent judges + overlays + SSE clients within one process
worker_class = "gthread"
timeout     = 3600       # Must be >= nginx proxy_read_timeout for SSE connections
keepalive   = 5
accesslog   = "-"        # stdout
errorlog    = "-"        # stderr
loglevel    = "info"
