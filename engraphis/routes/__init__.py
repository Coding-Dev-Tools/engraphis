"""Routes subpackage — v1 legacy and v2 routers share this flat package; know which
side a module belongs to before editing it (CLAUDE.md's "one rule", AGENTS.md §0):

v1 legacy (mounted by ``engraphis.app``, flat stores/engines namespaces):

* ``memory.py`` — the v1 REST memory API
* ``vault.py``  — the v1 vault/attachment routes

v2 (mounted by ``engraphis.dashboard_app``, built on ``core/`` + ``service.py``):

* ``v2_api.py``  — the dashboard/REST API over MemoryService
* ``v2_team.py`` — team-mode auth/user routes (AuthStore)

New capability goes on the v2 side behind ``core/interfaces.py``; the ``v2_`` filename
prefix is the convention that marks the target side in this package.
"""
