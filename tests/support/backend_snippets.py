"""Python one-liners that fake a long-lived backend process."""

SLEEP_SNIPPET = "import time; time.sleep(300)"

# The stock native backend binds its private endpoint socket only after exec,
# and the production launch waits for that bind, so the fake must really bind.
BIND_SLEEP_SNIPPET = """import socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind({path!r})
s.listen(1)
time.sleep(300)
"""
