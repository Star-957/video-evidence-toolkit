# Video Evidence Toolkit

Long-form video acquisition, visual-coverage validation, and offline regression
source. This archive contains authored tooling and historical versions, **not
videos, audio, subtitles, screenshots, model assets, research findings, API
credentials, collection approvals, or private review transcripts**.

## What is here

| Area | Included source |
| --- | --- |
| Long-form acquisition | Historical v1/v2 engines and the v3 entrypoint, shared core and policy module |
| Visual evidence | Chunk extraction, global frame-lattice checks, keyframe segmentation and attempt validation |
| Inventory | Approval-gated, single-channel raw YouTube API collection logic and mock-based tests |
| Local integration | Node launcher, Windows wrappers and an explicitly separate MCP smoke client |
| Regression coverage | Python contract tests and Node launcher/entrypoint tests |

The historical relative directory layout is preserved so sibling imports and
provenance checks remain inspectable. `SOURCE-INDEX.json` records the exact files
in this release; this is one source family, not a claim that every research
helper or every project has been archived.

## Evidence boundaries

A source hash, a frame count, a successful extractor, or a structural validator
does not prove that a person or model actually inspected every frame. It also
does not prove audio comprehension, independent reviewer coverage, factual
research conclusions, permission to collect media, or full-corpus completion.

Historical contracts and source pins must not be rewritten merely to make a new
validator accept them. The v1/v2/v3 implementations are separate versions. The
old protocol note is retained as historical documentation, not as a current
authorization or fresh test receipt. No original project, active contract,
receipt, approval file, or private dataset was modified to create this archive.

## Safe inspection

Start by reading the source and tests. Do not run acquisition, inventory, MCP,
or evidence-generation commands against an existing workspace as an archive
smoke test. Those commands can access networks or local files and can create
outputs. The omitted input data and approvals are intentionally not supplied.

The root npm package is marked `private: true` to prevent accidental registry
publication. The lockfile is dependency metadata, not bundled third-party code.
Dependencies are not installed by this archive's publication process. Some
dependencies have install scripts; the package's historical `allowScripts`
entries are not a new authorization to run them.

The Windows wrappers retain their historical Node installation path. The MCP
launcher blocks three specifically named environment keys, **not every possible
secret**. The opt-in smoke client starts the installed third-party server; it is
not part of the offline test claim. Review and deliberately restrict your
environment before choosing to use that integration.

## Validation for this archive

The publication check passed 144 selected offline tests: 5 Node launcher and
entrypoint tests, 104 Python acquisition tests, 26 visual-tool tests and 9
mock-API inventory tests. No selected test was skipped. Static checks also
parsed 19 Python files, 4 JavaScript modules, 1 PowerShell script and both package
JSON files. The command wrapper was covered by source inspection; only the
PowerShell wrapper was exercised with a disposable mock server script.

These tests used a fresh temporary profile/cache, a restricted environment and
trusted local FFmpeg/ffprobe/Pillow for short, generated CPU fixtures. `yt-dlp`
was absent from the test PATH, so its version lookup returned unavailable. No
actual video, private input, HTTP server, download, provider call, dependency
installation or real MCP server was used. Two localhost HTTP tests, a separate
inventory help test and `test:mcp` were deliberately excluded. This was not an
OS-level network sandbox or live end-to-end collection test.

An initial harness run passed four Node tests but failed the npm entrypoint
test because the harness supplied a forward-slash Windows shell path that npm
misclassified. After the harness used a native Windows path, the full selected
checks passed. That was a harness defect, not a change to the acquisition code.
The generated empty directories were backed up and moved outside this archive.

Known limits remain: the inventory approval validator checks manifest fields,
not authenticity of the referenced approval document; a receipt's refresh date
does not schedule deletion. The global lattice probe's production `-show_frames`
path has no explicit stream selection; its integration fixtures here were
video-only. Structural validation is not human/AI evidence review.

## Rights and scope

The owner identifies this as code developed in their AI-assisted projects;
this is not an independent authorship or rights certification. No blanket
license is granted by this archive. The root package uses `UNLICENSED` rather
than extending its former package-label license to an entire mixed archive.
Third-party dependency names and license metadata remain in the lockfile; no
dependency distribution, artwork or media asset is included.

Archive-copy changes: a nonexistent package `main` entry was removed,
accidental npm publication was disabled, a private evidence path was removed,
and a launcher test now uses a restricted child environment and a guaranteed
fixture-scoped missing executable instead of an unrelated absolute path.
Historical implementation logic remains unchanged. Original files and an
independently verified source backup are retained locally.
