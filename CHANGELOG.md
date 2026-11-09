# Changelog

## 0.3.0

- `tickwork replay RECORDS... --model M --provider openai-compat --base-url
  URL --key-env VAR` joins the CLI: it re-sends the `messages` array of each
  final transcript record in RECORDS to a model and prints one JSON metrics
  line per (record, model, repeat), scored by a built-in length/exact-match
  scorer. The `tickwork check` subcommand is unchanged.
- `tickwork.replay` joins the package: a generic, instance-neutral replay
  core — `read_records` (finals with full `messages` only), a `Provider`
  protocol with a `Reply` shape (response, usage incl. cache-hit tokens,
  `finish_reason`, `ms`, `error`), `replay(records, provider, models,
  scorers, repeats)` (one metrics dict per record × model × repeat,
  carrying the record identity `repo`/`issue` alongside `role`/`stage`,
  skips counted with reasons) and `deep_merge` (the recursive variant merge).
- `tickwork.pool` joins the package: stdlib, non-blocking `fcntl.flock`
  claim and worker-slot primitives (`try_claim`, `acquire_worker_slot`,
  `lock_worker_slot`, `flock_nonblock`). The state directory is a
  constructor/call parameter; the module is POSIX-only (it fails at import
  on platforms without `fcntl`).
- `tickwork.providers` joins the package with `openai_compat`, a stdlib
  (`urllib`) provider for any OpenAI-compatible `/chat/completions` endpoint;
  the base URL and the API-key env var name are constructor parameters and
  the key is never logged or printed.

## 0.2.0

- `tickwork.effect_log` joins the package: a generic, instance-neutral
  append-only JSONL effect log (plan → apply/mark with timing → resume
  unfinished effects), as an `EffectLog(log_dir)` object with one file per
  role. The log directory is a constructor parameter and `role` is a
  required argument with no default; the module imports only the standard
  library.

## 0.1.0

- First standalone release of the in-tree tickwork package: the workflow
  states loader/validator (`tickwork.workflow_states`), published as a
  generic, instance-neutral `tickwork` distribution with Python >=3.11 and
  PyYAML>=6.0 as its only dependencies.
- The package keeps importing only the standard library and PyYAML; callers
  supply the states file, the allowed owner roles and the role-to-login map.
