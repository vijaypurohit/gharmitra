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
