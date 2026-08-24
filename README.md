# become

*English · [한국어](README_KR.md)*

**A university in your hand.** become is a local-first personal learning system with one coordinator, five
specialist-agent contracts, evidence-backed learning paths, and time-based memory review. The specialist model
implements the responsibilities demonstrated in [this ALTER learning workflow](https://www.youtube.com/watch?v=lF8_DX2NxjI),
while the data and enforcement layer remain deliberately small and local.

```text
Orchestrator  workflow order, dependencies, interruption and resumption
├─ Advisor    destination, baseline, sequence, exclusions, proof milestones
├─ Librarian  source triage and a focused shelf of 3–4 materials
├─ Tutor      teaching, confusion diagnosis, connections, delayed retrieval
├─ Editor     version-bound critique and learner revision loop
└─ Roommate   questions through a deliberately different field
```

`AGENTS.md` is the coordinator contract, `agents/*.md` defines each specialist's isolated responsibilities and
permissions, and `become.py` stores and validates their outputs. It uses only the Python standard library.
Desktop sessions use the current agent host; `mobile.py` runs real specialist contexts through an isolated
Codex CLI workspace.

## Start

Ask the Orchestrator to learn something, or inspect the first route locally:

```bash
python3 become.py --actor orchestrator orchestrator route --intent learn
python3 become.py --actor orchestrator orchestrator workflow-start --intent learn --request "learn backpressure for production systems"
```

The workflow starts only the roles required by current state. A new learner normally follows
`Advisor → Librarian → Tutor → Advisor`; an existing ready shelf can start at Tutor. Editor and Roommate are
invoked when a deliverable or an outside-field perspective is actually useful.

## What each role actually does

Advisor interviews one decision at a time and must resolve five decisions before committing a curriculum:
destination, evidence-backed baseline, prerequisite order, a cut list, and milestones proven by learner work.
Tutor observations update this path, and previously resolved confusion can return as a remedial objective when
estimated retention decays. Every sequence step needs proof; only the active step can be completed, and its
evidence must belong to the current curriculum version before the next prerequisite-ready step opens.
Changing the study goal or core focus archives the old profile and curriculum, deactivates its scheduled
knowledge without deleting it, and starts a fresh five-decision path.

Librarian opens the source and separates reachability from content verification. Every candidate receives a
reasoned decision for relevance, credibility, level fit, signal density, and 1–5 priority, bound to a curriculum
version and step. Only verified, triaged signal enters a shelf, with the strongest 3–4 sources selected for the
active step. Byte-identical copies count once, and reachability plus the full content fingerprint are checked
again before use. Empty, too-basic, and too-advanced sources cannot enter it. Tutor cannot cite stale,
future-step, invalidated, changed, missing, or unselected material.

Tutor teaches new material before testing it, identifies the first point of confusion, and always explains why it
is used, why it took this form, why the result follows, and where it is applied. A connection must resolve to one
active related knowledge item in the current subject or one Advisor-committed curriculum baseline.
It stores the exact prompt, answer, rating rationale, confidence, weak points, and bidirectional concept links.
Same-titled concepts in different subjects keep separate memory state. A new-learning step requires
post-claim teaching followed by a different-case application review; a due step requires an unaided retrieval
review followed by why/connection teaching that addresses the answer. The first application review, not the row
creation or teaching timestamp, starts the forgetting clock.

Editor stores purpose, audience, and the learner's version. It reviews thinking, logic, evidence, repetition,
structure, precision, and accuracy. Each finding identifies the affected span, diagnosis, and learner action.
The learner submits the next version; Editor re-reviews it, resolves repaired findings, and links a returning
problem as `regressed`. Open
findings or failed milestone criteria cannot receive a pass, and `previous_versions` remains available for audit.
Whitespace- or Unicode-only resubmissions are not new learner revisions.
Version-3 artifacts without author provenance are quarantined as `legacy_unknown` and cannot be reviewed or used
as milestone evidence until the learner explicitly resubmits them.

Roommate is not a session checkpoint. It introduces a concrete mechanism from a different field, asks one
connection question, waits for the learner's answer, and records the mapping and where the analogy breaks.
Connections may explicitly end as `no_connection` or `needs_verification` instead of being forced.
Only one unanswered perspective can exist at a time, and the same lens/question pair cannot be replayed.

Orchestrator owns workflow order and session continuity. A dependent step cannot be claimed early, and a workflow
step cannot complete with a summary or an unrelated old id: it needs the expected output created or changed
after that step began. Completed results are copied into the next step's context. This is why the
coordinator exists—it enforces accountability without pretending to plan, teach, research, edit, or invent a
perspective itself. Manual handoffs use the same role-completion checks and take their snapshot at claim time.
If a parallel request changes the bound curriculum version, the stale workflow/handoff becomes
`superseded`/`cancelled`. The plan performing a goal/focus change rebinds to the new target; other stale work is
cancelled. Retention changes also re-route in-flight learning modes. One semantic output version cannot complete
two requests. Tutor must return observations and recommendations; the following Advisor must record that exact
observation as level evidence and link its goal to that knowledge id. Evidence records both the source Tutor handoff
and producing Advisor handoff, so one Tutor observation cannot satisfy two Advisor updates. A session linked by workflow id
resumes with the live workflow, current step, and handoff.
Every specialist write requires a claimed handoff. Different roles may run in parallel, but each role can have only
one in-progress handoff. Changed resources are stamped with that producing handoff, preventing composite or replayed
outputs; `--scope-handoff` can bind a command to it explicitly.
A manual Advisor handoff declares `curriculum` or `advisor_update`; Editor's artifact id is bound at dispatch.
Roommate receives only the current field/problem, creates the outside field, lens, and question itself, and binds
that perspective to the claimed handoff so it cannot be swapped before completion.

## Command examples

Specialist write commands below run only after that role claims an Orchestrator handoff.

```bash
python3 become.py --actor advisor advisor interview --decision destination --question "What must you be able to do?" --answer "Defend a queue-capacity decision with load evidence"
python3 become.py --actor advisor advisor curriculum --spec '{...}'
python3 become.py --actor advisor advisor recommend  # read-only; never writes state
python3 become.py --actor advisor advisor next       # write path; needs a claimed Advisor handoff
python3 become.py --actor librarian librarian add --title "official guide" --source "/path/to/source" --evidence "sections read directly"
python3 become.py --actor librarian librarian curate MATERIAL_ID --assessment '{...}'
python3 become.py --actor librarian librarian shelf --curriculum-id CURRICULUM_ID --step-id STEP_ID --candidate-id MATERIAL_1 --candidate-id MATERIAL_2 --candidate-id MATERIAL_3
python3 become.py --actor tutor tutor teach KNOWLEDGE_ID --explanation "왜 쓰는가: ... 왜 이렇게 되었는가: ... 왜 이 결과가 나오는가: ... 그래서 어디에 쓰는가: ..." --connection "stored related knowledge or curriculum baseline"
python3 become.py --actor tutor tutor review KNOWLEDGE_ID good --confidence complete --prompt "question" --answer "learner answer" --rationale "rating basis"
python3 become.py --actor learner editor add --title "deliverable" --content "draft" --purpose "decision" --audience "team"
python3 become.py --actor editor editor review ARTIFACT_ID --criteria '{...}' --milestone-criteria '{...}' --verdict revise --next "learner action"
python3 become.py --actor learner editor revise ARTIFACT_ID --content "learner revision"
python3 become.py --actor roommate roommate ask --current-field "distributed systems" --problem "backpressure" --outside-field "urban traffic" --lens "ramp metering" --question "Where should admission be limited?"
python3 become.py --actor orchestrator orchestrator session-start --context "commute" --workflow-id WORKFLOW_ID
python3 become.py --actor orchestrator orchestrator session-resume
python3 become.py --actor orchestrator orchestrator dispatch --to advisor --task "update path" --output-kind advisor_update
python3 become.py --actor orchestrator orchestrator dispatch --to editor --task "review current draft" --resource-id ARTIFACT_ID
python3 become.py --actor orchestrator orchestrator workflow-start --intent perspective --request "outside lens" --current-field "distributed systems" --problem "backpressure"
```

Use `python3 become.py --help` and each role's `--help` for the complete command surface.

## Memory and evidence

Personal state lives in `.become/state.json`; review audit events live in `.become/reviews.jsonl`. `.become/` is
ignored by Git. Every CLI and PWA writer uses the same inter-process lock and compare-and-swap; a local transaction
journal restores the previous consistent pair if the process exits while both files are changing. Set `BECOME_HOME`
to use another private directory.
Version-3 state is migrated without inventing provenance; malformed nested role or orchestration state already marked v4 is
rejected instead of silently normalized.

```text
estimated retention = 0.9 ^ (elapsed days / stability days)
```

An interaction before `due_at` is `exposure` and cannot increase stability. Only an independent answer after the
delay is `retrieval`. Partial or failed confidence requires a concrete weak point. Resolved confusion remains in
history so Advisor can schedule it again after likely forgetting.

## Mobile personal university

```bash
python3 mobile.py --home .become
```

Open `http://127.0.0.1:8765` for review and real ALTER-agent conversation. A message immediately returns a durable
job id; the single server queue continues the usual 1–3 minute role run after the page closes, and the PWA resumes
honest queued/running/completed/failed/interrupted events on reload. Each Codex run sees only a temporary copy of
the learning home. The host imports validated state and appended review events as one recoverable commit only when
the live fingerprint is unchanged, so a concurrent learner review or CLI write wins instead of being overwritten.

The mobile product has four areas: **Today**, **Curriculum**, **History**, and **My University**. Today keeps one
5/10/15-minute action above the fold. New learning renders the Tutor's four-why explanation, known-concept
connection, and example before asking for a different-case application. A due retrieval is a separate screen state:
the reference is neither sent nor placed in the DOM until the learner submits an answer or explicitly gives up.
Feedback locks the submitted answer, shows the saved weak point and targeted correction, then gives the exact next
due time. Curriculum exposes observable capabilities, active/completed/locked stages, proof criteria, and cut-list
reasons. History is paginated. My University contains learner-authored Editor artifacts and revisions, Roommate
connections with analogy limits, and value-oriented agent progress without raw handoff logs.

The shell plus sanitized current lesson/path/history/campus read models remain readable offline. Drafts and a bounded
answer outbox live only in browser storage; canonical truth remains `state.json` and `reviews.jsonl`. Each answer has
a stable request id and base interaction sequence. Reconnect sync returns the original receipt for a duplicate id,
keeps stale attempts visible as conflicts, and removes a queued answer only after the server acknowledges the single
canonical review event. API responses and hidden retrieval references are never service-worker cached. Shell updates
wait for the learner to choose a safe reload instead of mixing old and new assets.

Loopback needs no token by default. For an HTTPS/private-tunnel endpoint, use `--require-token` and set
`BECOME_MOBILE_TOKEN`. Non-loopback plain HTTP is refused unless `--allow-insecure-http` explicitly acknowledges
that Bearer authorization does not encrypt the token or learning data.

## Verify

```bash
python3 -m py_compile become.py mobile.py
python3 -m unittest -v
```

The suite covers role success and invalid transitions, access denial, workflow dependencies, populated-state
migration, source gating, memory timing, the local PWA, and a complete six-role journey.

## Structure

```text
become.py       local execution and state engine
mobile.py       isolated Codex runner, durable job queue, auth, and mobile API
mobile/         installable four-area personal-university PWA
AGENTS.md       Orchestrator contract
agents/         five specialist contracts
tests/          role, migration, and journey verification
.become/        Git-ignored personal data
```
