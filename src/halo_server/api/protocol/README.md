# Halo Plugin Protocol

Halo Server owns the canonical wire contract used by integration plugins.

Each version directory contains:

- `schema.json`: the JSON Schema for every server-to-plugin JSON payload
- `fixtures.json`: valid and invalid messages consumed by server and plugin tests

The schema and fixtures ship in the Halo Server wheel. A plugin repository may vendor an exact
copy, but must record the source Halo Server commit and the SHA-256 digest of both files.

Any wire change must update the canonical schema and fixtures. A breaking change requires a new
protocol version; existing version directories are immutable after release.

Protocol v3 is the current server contract. Protocol v2 remains available as immutable release
history and must not be edited.

Fixtures may mark an invalid message with `schema_valid: true` when the rejected condition is a
cross-field semantic rule that standard JSON Schema cannot express, such as requiring one state
entry for every advertised device ID. Consumer parsers must test those cases explicitly.
