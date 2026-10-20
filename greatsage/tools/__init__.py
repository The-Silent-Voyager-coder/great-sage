"""Great Sage secure tool subsystem (Phase 4).

Provider-independent tool abstraction: registry, typed requests/results,
JSON-mappable schemas, risk levels, security policy with ALLOW/DENY/ASK
decisions, approval providers, audit events, and a foundational tool set
(filesystem, process, system, shell).

Submodules are imported lazily by the runtime to avoid import cycles; this
package intentionally exposes no eager imports.
"""
