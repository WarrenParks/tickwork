# tickwork

A generic, instance-neutral workflow engine: it loads a declarative
"states-as-data" file, validates it, and evaluates which state each work item
in a queue of issues and pull requests is in — including a per-item anomaly
report when an item matches zero or several states. It knows nothing about
any specific deployment or consumer: callers supply the states file, the set
of allowed owner role names, and the role-to-login mapping.

## Install

```bash
pip install tickwork
```

Requires Python 3.11+ and PyYAML.

## States as data

A states file maps each state to a `when` predicate, a board bucket, and an
`owner` (a role name, or `human`). Generic role names only — the file, like
the package, names no instance:

```yaml
states:
  queued:
    when:
      issue.assignee: none
    board: queued
    owner: dev
  in_review:
    when:
      pr.requested_reviewers: reviewer
    board: blocked
    owner: reviewer
  waiting_on_human:
    when:
      any:
        - pr.state: merged
        - issue.state: merged
    board: waiting_on_human
    owner: human
  done:
    when:
      issue.state: closed
    board: done
    terminal: true
```

Load, validate, evaluate, and report anomalies:

```python
import tickwork.workflow_states as workflow_states

states = workflow_states.load("states.yaml", roles={"dev", "reviewer", "planner"})

item = {...}  # repo, issue (number/state/assignees/open_dependencies), pr
logins = {"dev": "d", "reviewer": "r"}
mapped = workflow_states.evaluate(states, item, logins=logins)

items = [item]
anomalies = workflow_states.check(states, items, logins=logins)  # zero- or 2+-matched items
```

## Effect log

`tickwork.effect_log` is an append-only JSONL log of intended and applied
writes ("effects"): a caller plans one pending row per effect *before*
applying any of them, then each row is marked applied / failed / superseded
with timing. On the next run the caller folds the log and resumes rows that
are still unfinished, instead of re-deciding a job it can no longer finish.
The module names no deployment: the log directory is a constructor parameter
and every call names its role explicitly — there is no default role.

```python
from tickwork.effect_log import EffectLog

log = EffectLog("/var/lib/myapp/effects")  # one <role>.jsonl per role

rows = log.plan("repo#7", "run-42", [("comment", {"body": "..."})], head_sha="abc", role="bot")
log.apply(rows[0], lambda: post_comment(rows[0]["payload"]))   # marks applied/failed with timing
log.mark(rows[0], "superseded")                                # void a row without a write

resume = log.unfinished("bot")  # {item: [pending rows in seq order]}
```

`tickwork.effect_log.summarize(rows)` reads the same log back and returns a
JSON-able dict: per `(role, effect)` counts of `applied` / `failed` /
`superseded` / `pending` after folding (the latest status row per
`(item, tick_id, seq)` wins), nearest-rank `p50`/`p90` of `ms` over folded
`applied` rows (`None` where no measured write exists), a `finished_later`
count of fold keys whose `failed` status was followed, later in log order, by
an `applied` one (a resume finished it), and the still-`pending` rows. The
companion command renders that object read-only, never creating or writing
the log directory, and exits 0 on a missing or empty one:

```bash
tickwork effects /var/lib/myapp/effects                       # every <role>.jsonl
tickwork effects /var/lib/myapp/effects --role bot            # one role
tickwork effects /var/lib/myapp/effects --json                # one JSON object
tickwork effects /var/lib/myapp/effects --since 2026-07-01
```

## Checking a queue

A tickwork install carries two commands:

- `tickwork check STATES --items ITEMS --logins LOGINS` — report items
  matching zero or 2+ states
- `tickwork replay RECORDS... --model M --provider openai-compat
  --base-url URL --key-env VAR` — re-send and score the recorded LLM calls
  in *RECORDS* (final transcript records, one JSONL object per line, each
  carrying `final: true` and the full `messages` array) against a model and
  print one JSON metrics line per (record, model, repeat). The metrics line
  carries the record identity (`repo`, `issue`, `role`, `stage`), the
  per-call facts (`ms`, token usage, `finish_reason`, `error`)
  plus the built-in scorer's `response_len` and `exact_match` fields: the
  reply's length and whether it matches the recorded response exactly.
  Exit code `2` means a file could not be read, an unknown `--provider` was
  given or `--repeats` was below 1; a run with zero scored and zero skipped
  records exits `1`.

`STATES` is a states file in the format shown above, `ITEMS` is a JSONL file
with one item per line, and `LOGINS` is a YAML mapping of role name to login.
The package ships three example files under `examples/`:

- `examples/pr-loop.states.yaml` — a states file (see "States as data").
- `examples/items.jsonl` — three sample work items.
- `examples/logins.yaml` — the role-to-login map the predicates resolve
  through.

Every item line in `ITEMS` is a JSON object with a `repo` name, an `issue`
map (`number`, `state`, `assignees`, `open_dependencies`) and either a null
or a `pr` map (`number`, `state`, `assignees`, `requested_reviewers`,
`reviewed_on_head`, `approved_on_head`). The example items are contrived so
that one is clean, one matches no state, and one matches two at once, so a
run over them reports two anomalies and exits `1`. After an install, the
three files live under `<sys.prefix>/share/tickwork/examples/`; from a
source checkout, the same files sit at `release/tickwork/examples/`, and
every path below works with either location:

```bash
tickwork check examples/pr-loop.states.yaml \
    --items examples/items.jsonl --logins examples/logins.yaml
# pip-installed: point the arguments at the installed share directories, e.g.
# tickwork check "$(python -c 'import sys; print(sys.prefix)')/share/tickwork/examples/pr-loop.states.yaml" ...
```

The check prints one line per item that matches zero or more than one state
(an anomaly: an item no state owns, or two states contest), then a summary
line. Exit code `1` means at least one anomaly was found, `0` means the
input was clean, and `2` means a file could not be read, parsed or validated.

## Replaying recorded calls

```bash
tickwork replay issue-42/20260915-reviewer-review-final.jsonl \
    --model my-model --provider openai-compat \
    --base-url https://example.internal/v1 --key-env MY_API_KEY
```

The provider adapter is stdlib-only (`urllib`) and speaks any
OpenAI-compatible `/chat/completions` endpoint: the base URL and the name of
the API-key environment variable are flags, not baked-in endpoints. The key
is read from the environment and used only in the request's `Authorization`
header — it is never logged or printed. A provider failure (bad URL, missing
or unset key, refused connection) does not abort the run or change the exit
code: it rides in the metrics line's `error` field, so an errored call is
still one printed line with `response: null`. `--provider` names the adapter
(`openai-compat`; that default is used when the flag is omitted), and
`--repeats N` (a positive integer) sends each recorded call N times.

For library use the replay core is provider- and scorer-agnostic — callers
supply both:

```python
import tickwork.replay as replay

records = replay.read_records(["run-final.jsonl"])
skips: dict[str, int] = {}
for metrics in replay.replay(
    records,
    provider=my_provider,             # .generate(model, messages) -> reply
    models=["model-a", "model-b"],
    scorers={("*", "*"): my_scorer},  # (record, reply) -> extra metrics keys
    repeats=2,
    skips=skips,                      # counted skip reasons
):
    print(metrics)
```

`replay.deep_merge(base, overlay)` is the recursive variant-overlay merge
(nested mappings merge key by key; other values are replaced) — use it to
compose a config variant before replaying under it.

## Worker pools

`tickwork.pool` provides the concurrency primitives a worker pool needs,
built on non-blocking `fcntl.flock`: no waiting, no daemon, and no explicit
cleanup — the kernel releases the lock when the holding process exits, so a
crashed worker never leaves a stale lock behind. Four functions:

- `try_claim(role, state_dir, kind, repo, number)` — claim exclusive
  ownership of one item (e.g. `kind="issue"` or `"pr"`) so two concurrent
  ticks of the same role never work the same item. Returns a context
  manager holding the claim, or `None` when another process already holds
  it.
- `acquire_worker_slot(role, state_dir, pool_size)` — claim the
  lowest-numbered free slot in `1..pool_size` for per-tick disambiguation
  (branch naming, local state). Returns a context manager yielding the slot
  number, or `None` when every slot is taken — skip the tick rather than
  wait.
- `lock_worker_slot(role, state_dir, slot)` — claim a *specific* slot's
  lease (no free-slot scan), for when the slot is pinned from outside the
  process.
- `flock_nonblock(path)` — open *path* and take a non-blocking exclusive
  `flock`; returns the file descriptor, or `None` when another holder has
  the lock. Public so a caller can hold an extra lock file next to the
  generic ones.

Lock files live under `<state_dir>/claims/<role>/...` (one per claimed
item) and `<state_dir>/slots/<role>/<slot>.lock` (one per worker slot). The
state directory is a call parameter and the module reads no environment
variables, so it names nothing of the instance it runs for. The module is
POSIX-only: it fails at import on platforms without `fcntl`.

```python
import tickwork.pool as pool

claim = pool.try_claim("worker", "/var/lib/myapp/state", "issue", "owner/name", 42)
if claim is None:
    return  # another tick already owns issue 42 — skip this one
with claim:
    ...  # compose the work; the claim is released on exit or crash
```
