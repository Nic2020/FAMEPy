# Security and privacy

Report security issues privately to `statespaceecon@gmail.com`. Please provide
a synthetic reproduction and omit credentials, private databases and raw logs.
This project is in its first release-candidate cycle; no production support
or response-time guarantee is currently offered.

Importing FAMEPy does not load FAME. Library discovery uses explicit absolute
paths or the configured FAME installation, not a current-directory/PATH search.
Only point the optional probe at trusted libraries: loading a native library
executes its loader code. A subprocess limits the effect of a crash; it is not
a security sandbox.

Diagnostic reports intentionally omit paths, hostnames and raw loader output.
Review reports before sharing them. FAMEPy does not transmit reports or perform
network requests of its own. Opening a database through a connection string
is an explicit user operation on the caller's already configured route; the
string is passed to the library unchanged and never appears in diagnostics,
`repr` or error messages. No vendor runtime, credentials or databases are
bundled.
