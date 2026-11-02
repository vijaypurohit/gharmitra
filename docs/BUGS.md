# Bugs found while building

Each entry: what broke, why, the fix, and the regression test that guards it.

## 1. Bundled Postgres would not start from a folder with a space

- **What broke**: `uv run pytest` errored on every database test with
  `postgres: invalid argument: "Projects/gharmitra/data/pg"` when the repo lived in
  `~/Documents/DEV Projects/`. A first check had passed because it used a long temp path.
- **Why**: pgserver puts the Unix socket in the data directory when the path is short enough, and
  passes it to postgres as `-o "-k <dir>"` without quoting, so the path was split at the space. With a
  long path it falls back to a runtime directory, which is why the first check worked.
- **Fix**: `db._patch_socket_dir()` wraps pgserver's socket-directory choice and falls back to its
  runtime directory whenever the chosen path contains whitespace.
- **Regression test**:
  [`test_bug_1_bundled_postgres_path_with_spaces`](../tests/unit/test_foundations.py)

## 2. Trace redaction corrupted approval fingerprints

- **What broke**: invariant I1 failed in evals E6 and E26 with "fingerprint differs from approval",
  although the gate had passed. The fingerprint in the trace no longer matched the database row.
- **Why**: the redactor's phone-number pattern only refused a *digit* before or after the match, so a
  run of ten digits inside a SHA-256 hex string (e.g. `…a3be9845012345f…`) looked like an Indian
  mobile number and became `[phone]`. Any id with a long digit run was at risk too.
- **Fix**: the pattern now requires a non-word character on both sides (`(?<![\w-])…(?![\w-])`), so
  it only matches a number standing on its own.
- **Regression test**:
  [`test_bug_2_redaction_keeps_fingerprints_and_ids`](../tests/unit/test_foundations.py)
