import os

from gunicorn.glogging import Logger


class AccessLogger(Logger):
    """Suppress successful health-check access logs."""

    def access(self, resp, req, environ, request_time):
        """Log every request except successful health checks."""
        status = str(resp.status).split(maxsplit=1)[0]

        if (
            environ.get("REQUEST_METHOD") == "GET"
            and environ.get("PATH_INFO") == "/healthz"
            and status == "200"
        ):
            return

        super().access(resp, req, environ, request_time)


# Runtime settings are configurable so the same image works as a standalone
# service or as a sidecar sharing a pod network namespace.
bind = os.environ.get("HTML2PDF__LISTEN_ADDRESS", "0.0.0.0:8080")
workers = int(os.environ.get("HTML2PDF__WORKERS", "2"))
timeout = int(os.environ.get("HTML2PDF__TIMEOUT", "45"))

# Give workers a short window to finish requests during graceful shutdown.
graceful_timeout = 10

# Keep all Gunicorn runtime files on the writable temporary filesystem.
worker_tmp_dir = "/tmp"

# Send request and application logs to the container's stdout/stderr.
accesslog = "-"
errorlog = "-"

# Suppress only successful GET /healthz entries. Errors and other requests
# continue through Gunicorn's normal access logger.
logger_class = AccessLogger

# Avoid creating a Unix control socket in the otherwise read-only container.
control_socket_disable = True
