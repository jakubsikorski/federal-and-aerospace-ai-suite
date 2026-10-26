"""Signal shutdown before draining SSE requests or waiting for startup."""
import uvicorn

from . import pipelines


class Server(uvicorn.Server):
    def handle_exit(self, sig, frame):
        pipelines.shutdown.set()
        super().handle_exit(sig, frame)


if __name__ == "__main__":
    Server(uvicorn.Config("app.main:app", host="0.0.0.0", port=8100,
                          timeout_graceful_shutdown=5)).run()
