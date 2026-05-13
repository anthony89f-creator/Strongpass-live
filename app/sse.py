import threading

_sse_condition   = threading.Condition()
_sse_version     = 0
_results_version = 0  # increments only when scores/results data changes


def _sse_notify():
    """Wake all SSE clients after a state change. Call outside any state_lock."""
    global _sse_version
    with _sse_condition:
        _sse_version += 1
        _sse_condition.notify_all()
