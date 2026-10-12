# Keep the UI responsive even if one request is waiting on SQLite or an external API.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 30
graceful_timeout = 10
keepalive = 5
