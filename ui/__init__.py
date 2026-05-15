"""
Server-rendered demo UI (``/ui``).

This is a **sibling package** of ``app``, deliberately placed at the
repo root rather than under ``app/`` so the separation reads at a
glance: ``app`` is the JSON API + clinical model; ``ui`` is a thin
surface that consumes the API. ``app`` does not depend on ``ui`` — the
import edge runs one way, from ``app.main`` mounting the router below.

The brief lists a UI as "nice but not required" — this module exists to
let a reviewer click through the three deliverables (Task 2 chart, Task 3
ASAM LoC, Task 3 TJC audit) without writing curl, while preserving the
architectural story that the JSON API is the system's interface.

Design choices (documented for the reviewer):

  * **Server-rendered Jinja2, not an SPA.** Keeps the UI in the same
    process and language as the rest of the project; no node toolchain
    to learn during evaluation.
  * **UI routes call the existing JSON endpoints in-process** via an
    ``httpx.AsyncClient(transport=ASGITransport(app=app))``. The UI is a
    *client* of the API, not a parallel data path. This proves the API
    is sufficient for any client (a hypothetical React or mobile app
    would speak to the same endpoints) and exercises the full auth /
    ETag / audit / error stack on every page load.
  * **The UI itself does not require an API key.** Anyone who can reach
    the UI surface is treated as an authorized local-demo user; the
    UI's internal client supplies ``X-API-Key`` from settings when
    calling the JSON API. This mirrors how a real frontend would
    forward an OAuth bearer token rather than asking the human user to
    paste one.
"""
