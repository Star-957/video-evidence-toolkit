# Historical long-form acquisition protocol (v3)

This is an archived implementation note, not a new collection authorization or
current research-status receipt. Paths below describe the historical source
layout. Use an isolated checkout and independently approved inputs; do not run
these commands against an existing research workspace merely to test this archive.

The maintained default for new work is `npm run acquire:longform -- ...` from
`C:\NeuroVideoAudit`. The package command directly runs
`research/full-channel-2026-08-16/scripts/acquire_longform_sections_v3.py`;
there is no additional router or copy of the engine.

```powershell
Set-Location C:\NeuroVideoAudit
npm run acquire:longform -- init --help
npm run acquire:longform -- init --url-file '<exact URL text file>' --identity-file '<identity JSON>' --output-dir '<new non-existent output directory>'
```

`init` reads those local input files and creates a new contract without
downloading media. Its `--policy video-only-v2` default retains video only,
as required by the active 2026-08-29 research contract. `--policy legacy-v1`
is reserved for an explicitly selected historical/compatibility scenario; it
does not authorize retaining audio in active video-only research. Every later
`acquire`, `acquire-full`, `verify` and `complete` command reads the policy from
that v3 contract. CLI flags otherwise retain the prior forms. Existing rights
and approval gates remain in force.

The v3 entry, shared core, and policy module are all SHA-256-bound in each v3 contract. Source changes after import, any closure hash mismatch, changed settings and policy mismatch fail before acquisition. Both policies run the same planning/publication/duration/tamper/completion regression cases.

Existing v1/v2 scripts and receipts are unchanged. V3 uses
`longform-source-segments-v3`; it does not rewrite or adopt historical
contracts. Both historical engines emitted schema-v1, so schema alone cannot
select the correct verifier: resume/verify requires the engine matching the
contract's `pipeline_provenance.script_sha256`. Unknown or missing pins remain
blocked. Do not migrate, copy, rehash or relabel old contracts to make them
pass v3 verification. No acquisition or historical completion-state update
was performed for this adoption.

Offline verification, from `C:\NeuroVideoAudit`:

```powershell
node --test tests/longform-entrypoint.test.mjs
npm test
python -B -m unittest discover -s research/full-channel-2026-08-16/tests -p 'test_acquire_longform_sections*.py'
```

The new entrypoint test invokes the actual npm route for help, a synthetic
default-policy init and rejection of a synthetic legacy-schema receipt without
rewriting it. The root Node suite includes that test and four launcher tests.
The 104 Python acquisition tests include 70 v3 cases covering both policies
and both acquisition paths up to the external I/O boundary. Media tests use
only disposable generated CPU media and injected file-copy acquisition
runners; no real MCP server, network download or model is needed. Temporary
fixtures are removed after the tests.

The original adoption receipts and private workspace paths are not included in
this source archive. The historical test counts above are not fresh validation
results for this copy. Consult the root README for checks actually performed on
the archived revision. Source publication is not acquisition, visual review,
Council evidence review, or full-channel completion.
