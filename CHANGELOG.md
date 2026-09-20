# Changelog

Versions follow [PEP 440](https://peps.python.org/pep-0440/). A version
is built and verified once by a rehearsal run whose artifact is reviewed,
and only that reviewed artifact is promoted to the index (see
[releasing](docs/releasing.md)). An entry marked *unreleased* has not been
promoted.

## 0.1.0rc1 (unreleased)

First release candidate. FAMEPy is a pure-Python package that binds the
FAME CHLI library through `ctypes`, with the behavior of
[FAME.jl](https://github.com/bankofcanada/FAME.jl) as the reference and
[TimeSeriesEconPy](https://github.com/Nic2020/TimeSeriesEconPy) as the
time-series model. A separately installed, licensed FAME runtime is
required for every native operation; nothing of it is distributed.

Included:

- Runtime lifecycle: one initialization per process, terminal
  finalization, trusted library discovery through `FAME` or
  `FAMEPY_LIBRARY`, diagnostics and a subprocess symbol probe without
  native calls.
- Databases: the five local access modes, the work database, explicit
  posting, connection strings passed through the local open (the
  reference's read-only remote route); the server-connection write modes
  are refused before any native call, as neither wrapper binds them.
- Raw object I/O for precision, numeric, Boolean, date, string and
  namelist scalars and series with preserved missing categories,
  subranges, exact-width scalars and complete validation before the first
  native call; wildcard listing with filters; command execution with
  recursive `INPUT` expansion; opt-in extended error text.
- The TimeSeriesEconPy bridge: every reference value kind, every reference
  frequency anchor, workspace, mapping and multivariate writes, workspace
  reads with name transformation, per-object reporting variants, and the
  missing, empty and text policies. String values are ASCII by default and
  can be exchanged as raw bytes or, opt-in, as strict UTF-8; names, paths
  and commands stay ASCII.
- A FAME-to-DataEcon migration workflow with a plan-first, refuse-by-default
  loss policy and structural verification on read.
- A consolidated native validation runner (eleven groups, isolated worker
  processes, sanitized reports, optional FAME.jl differential checks) and a
  benchmark harness with verified read-backs.

Evidence for this candidate is summarized in
[capability status](docs/capabilities.md) and the
[parity ledger](docs/parity.md): the eleven validation groups passed on one
Windows and one Linux installation with the FAME.jl comparisons configured
on both, the benchmark scenarios completed on both, and a bounded read-only
comparison of one small approved remote selection was equal through both
wrappers on both hosts. These are statements about the inspected
installations, not blanket version coverage.

Deliberate differences from the reference are documented in
[contracts](docs/contracts.md) and the ledger: no restart of the runtime in
one process, missing Booleans refused instead of read as `true`, a scalar
NaN written as NC, integer scalars kept exact or refused, an explicit mode
required for path writes, and whole-value UTF-8 decoding instead of the
reference's byte-length slicing.
