# Consultation Non-Repetition Design

## Status

Approved by the user in sections on 2026-08-13.

## Problem

The consultation system has several independent continuation paths:

- formal Playbook cases;
- unverified-topic guidance;
- emergency guidance;
- required-fact collection;
- new-case routing;
- restored historical consultations.

Those paths do not share a result-level progression rule. A provider can
return the previous answer again, an unverified topic can rebuild the same
static guidance, and fact collection can ask the same question again. The
result is especially visible when the user adds information or sends a short
message such as `continue`: a new turn can contain the same answer, or a
lightly reworded version of it.

Prompt instructions alone cannot guarantee non-repetition. Every response
must pass a deterministic backend guard before it can become a consultation
turn.

## Goals

- Never persist or display a new assistant turn whose useful content merely
  repeats an earlier answer in the same consultation.
- Treat an exact copy and a superficial rewording as repetition.
- Make every accepted continuation do at least one of the following:
  - explain the effect of a newly supplied fact;
  - directly answer the user's new question;
  - advance to an unfinished action;
  - ask one more precise, previously unasked question;
  - identify a genuinely separate case;
  - react to a changed safety state.
- Give `continue` a deterministic meaning: advance when possible, otherwise
  identify the one missing condition needed to advance.
- Apply the rule to every formal Playbook, every current unverified topic,
  emergency handling, fact collection, and restored history.
- Preserve the complete latest formal plan for case recovery while avoiding a
  repeated full-plan display after a small update.
- Avoid a database migration and remain compatible with existing consultation
  records.
- Avoid extra DeepSeek calls when correcting duplicate output.

## Non-Goals

- Do not change legal rules, Playbook conclusions, verified statute content,
  provider selection, account policy, or consultation quotas.
- Do not promise that every user message changes the legal conclusion. When a
  new fact does not change the conclusion, the response must say so briefly
  and still advance the practical handling of the case.
- Do not rewrite or delete old turns that already contain repetition.
- Do not use semantic embeddings, an additional model call, or an external
  similarity service.
- Do not turn an unverified topic into verified legal advice.
- Do not repurpose the existing `followup_round` database field as a general
  case-stage counter. It remains the bounded required-fact collection count.

## Core Invariant

A continuation may be persisted only when its visible core contains material
progress that is absent from all earlier assistant turns in the same
consultation.

The visible core consists of:

- the direct reply or change summary;
- actionable steps;
- evidence requests;
- the current communication instruction;
- the next question.

Static coverage notices, required limitations, citation labels, and a short
mandatory emergency safety anchor are not progress by themselves. They may be
repeated only when the response also contains a new visible-core unit.

## Architecture

### History Projection

Add a parser that converts every historical response shape into a compact
`VisibleTurnContent` representation. It must understand current and legacy
forms of:

- plans and plan updates;
- short follow-up replies;
- fact questions;
- unverified guidance;
- emergency guidance;
- new-case notices.

The parser is defensive. An old or partially projected response may omit a
section, but must never make the new request fail merely because that section
cannot be compared.

The complete consultation history is checked, not only the most recent three
turns. Each turn is projected once into field-length-bounded normalized units
and fingerprints. The implementation does not copy raw historical messages
into logs.

### Progress Analyzer

Add a single progress analyzer that receives:

- the current user message and confirmed attachment evidence;
- the current session facts;
- the routed topic or formal Playbook;
- all historical turns;
- the provider's extracted facts or continuation result when a provider call
  is needed.

It classifies the current request into exactly one progression outcome:

- `new_fact`;
- `direct_question`;
- `completed_action`;
- `continue_case`;
- `more_precise_question`;
- `new_case`;
- `risk_changed`;
- `no_progress`.

Deterministic messages such as `continue`, `then what`, `next step`, and an
exact repeat of the previous user message are handled before a provider call
when no attachment or new fact is present. This both improves consistency and
avoids an unnecessary paid request.

The analyzer derives stage from existing turns and facts. For an unverified
case, it recovers the topic from the latest compatible historical coverage
record and derives served stages from the historical visible core. A message
that clearly changes to a different dispute is not allowed to overwrite that
identity. No topic or stage column is added to the session table.

### Candidate Generation

Candidate generation remains specialized by consultation path, but each path
must return a progression outcome and visible-core content:

- formal cases use the locked plan, confirmed facts, action references, and
  provider continuation when needed;
- unverified topics use a deterministic stage-aware profile;
- fact collection selects one unanswered required fact;
- emergency handling prioritizes the current safety state;
- new-case routing remains isolated from the current case.

A provider answer is a candidate, not a final response. Provider identity,
slot, scenario, action-reference, and citation-reference validation continues
to run before non-repetition validation.

### Non-Repetition Guard

Add one backend guard shared by every path. It compares the candidate's
visible core with every historical visible core using:

1. Unicode NFKC normalization, lowercase Latin text, and removal of
   whitespace, punctuation, and presentation-only numbering.
2. Exact normalized equality.
3. Near-duplicate text detection for core strings of at least 24 characters:
   a sequence similarity of at least `0.90`, or character-trigram Jaccard
   similarity of at least `0.82` with no new fact, stage, action, or question.
4. Structural duplication: the normalized action, evidence, communication,
   and question sets are unchanged and the candidate has no fact delta or
   stage transition.
5. Repeated questioning: the selected question asks for the same fact already
   requested in an earlier turn without becoming more specific.

Common safety and legal phrases are excluded from the similarity numerator,
but they never count as novel content. A candidate must contain at least one
novel core unit.

### Deterministic Repair

When the guard rejects a provider or builder candidate, it does not call
DeepSeek again. A deterministic repair table produces one of:

- the next unfinished action;
- the practical effect of a confirmed new fact plus the next action;
- one previously unasked concrete question;
- the current blocker and the external change required before proceeding;
- a one-sentence safety anchor plus a new safety-status question;
- a separate-case notice.

The repaired result passes through the same guard once. The repair table is
total for all progression outcomes. If an implementation defect produces no
valid repair, the request fails closed and no consultation turn is written.

### Atomic Persistence Recheck

The pipeline guard prevents normal duplicates, but two concurrent requests
could otherwise compare against the same old history. The repository must
therefore recheck the accepted visible core inside the write transaction. The
commit command carries the latest turn ID observed by the analyzer and the
candidate's compact comparison units:

- PostgreSQL locks the session row, verifies the expected latest turn, reads
  any newly committed response when needed, and then inserts the new turn.
- SQLite starts its existing serialized write transaction and performs the
  same expected-latest-turn and equivalence checks before insertion.

If another request has already persisted equivalent progress, the second
write is rejected with `case_no_progress`. It does not insert a user message,
assistant response, or attachment binding. If the concurrent turn is
different rather than equivalent, the stale request is rejected with a
retryable consultation-conflict response so it can be regenerated against the
new history. This requires no new table or column.

## Consultation Path Behavior

### Formal Playbook Cases

A first formal result remains a complete plan.

For a continuation:

- New confirmed facts are merged and validated as they are today.
- The newly built canonical plan is compared with the latest canonical plan.
- If the plan materially changes, persist the complete new plan for recovery
  and return a concise change summary naming the changed fact and changed
  sections.
- If the plan does not materially change, persist the new facts but return a
  short fact-impact reply and the next unfinished action instead of presenting
  the same plan again.
- A concrete user question receives a direct answer before any suggested
  action.
- A bare `continue` selects the next unfinished action from the locked case
  and prior continuation history without calling the provider.
- A repeated question with no new fact returns the no-progress response. The
  interface points to the existing answer and asks the user to identify what
  is unclear; it does not create another answer turn.

For backward compatibility, a new `plan_update` may retain its complete
canonical `plan` and `verdict` payload. It also carries a concise `reply`
describing only the update. The web interface shows the update reply first and
keeps the complete updated plan collapsed for optional inspection. Old
`plan_update` records without a reply continue to render.

### Unverified Topics

The first response may contain the topic's full conservative guidance. Later
turns for the same topic use short follow-up replies rather than rebuilding
the same guidance object and communication template.

The latest compatible coverage record is authoritative for the current
unverified topic. If old history has no usable coverage record, the current
message is routed normally and starts a fresh guidance sequence; the system
must not guess a topic from an assistant sentence.

All current entries in `UNVERIFIED_TOPIC_IDS` use the same ordered stages:

1. safety confirmation;
2. evidence organization;
3. first written contact;
4. waiting for and recording a reply;
5. reminder with a clear response date;
6. escalation through the profile's channel;
7. professional assistance when the prior channels are exhausted.

User statements such as already contacted, no reply, refused, already
complained, or risk resolved may jump directly to the corresponding next
stage. A bare `continue` advances to the next unserved stage. Topic-specific
profiles still supply recipients, evidence, channels, and escalation
destinations, but only the material for the current stage is displayed.

After stage seven, another content-free `continue` produces
`case_no_progress` instead of another reworded final instruction.

### Required-Fact Collection

Each fact-collection turn asks one question.

Question selection excludes facts already confirmed and questions already
asked. When a required fact is still missing after a broad question, the next
question must identify the expected form of the answer, such as a date,
amount, document status, or yes/no condition. It cannot merely paraphrase the
same broad wording.

The existing two-round collection limit remains. Once the limit is reached,
the system gives the existing conservative limitation once. A further
content-free request returns `case_no_progress` and does not append the same
limitation again.

### Emergency Guidance

The first detected emergency continues to return complete safety-first
guidance and skips ordinary routing.

If the same risk is reported again:

- do not return the full emergency template;
- retain at most one short mandatory safety action;
- ask one new question about whether the user reached safety, obtained care,
  stopped payment, or preserved the at-risk evidence, according to the risk;
- advance to the next safety action when the user reports completion.

When the user reports that the immediate risk is resolved, processing returns
to the preserved formal case or the appropriate unverified stage. An
emergency interlude must not reset formal facts, plan state, or fact-collection
rounds.

### Separate Cases

A genuinely different dispute still produces a `new_case` response and does
not modify the current case. Repeating the same separate-case description
does not append the same notice again; the interface offers the existing
new-consultation action without creating another turn.

## No-Progress Handling

`case_no_progress` is a safe conflict response, not an assistant answer. It is
used only when:

- the user supplies no new information;
- no unserved action or more precise question remains; and
- any new answer would repeat an existing visible core.

The API returns HTTP `409` with a concise message asking for one of the
specific external changes relevant to the case, such as a reply from the
other party, a requested document, a new event, or a changed safety state.
The frontend keeps the user's draft available, does not append a user or
assistant bubble, and may focus the most relevant earlier answer.

Preflight no-progress detection happens before provider quota reservation.
When a provider has already been called and its output is repaired locally,
the single real provider call is still accounted for; no retry is made.

## Data and API Compatibility

- No database migration is required.
- Existing response JSON remains readable.
- New formal plan updates may add a `reply` beside the existing full plan.
- Unverified follow-ups may use the existing `followup_answer` shape with no
  formal coverage object.
- `followup_round` remains between zero and two and keeps its existing fact
  collection meaning.
- Existing history is not rewritten. The guard uses it when evaluating all
  future turns.
- History restoration must preserve the concise plan-update reply when
  present and continue projecting legacy records without one.

## Logging and Privacy

Add non-sensitive audit fields for:

- progression outcome;
- inferred stage;
- duplicate reason (`exact`, `near_text`, `same_structure`, or
  `repeated_question`);
- whether deterministic repair was used.

Do not log raw user messages, answer text, email addresses, attachment text,
facts, or communication drafts. Similarity diagnostics may log only lengths,
threshold-independent counters, and one-way fingerprints.

## Verification

All automated consultation tests use the Fake Provider. They must not call the
real DeepSeek API.

### Unit Tests

- Normalization treats Chinese and ASCII punctuation, whitespace, numbering,
  and letter case consistently.
- Exact, near-text, structural, and repeated-question duplicates are caught.
- Shared mandatory safety or limitation text does not make an otherwise new
  answer fail.
- A new fact, new action, new stage, or genuinely different question is not a
  false positive.
- Legacy response shapes project safely.
- Deterministic repair always produces a valid progression result.

### Pipeline and API Tests

- A Fake Provider returning the previous formal answer is repaired before
  persistence.
- A fact that changes the plan produces a concise update and stores the full
  latest canonical plan.
- A fact that does not change the plan produces a fact-impact reply rather
  than another full plan.
- Consecutive `continue` messages advance formal actions without duplicate
  answers.
- Every current unverified topic advances through distinct stage output.
- `already sent`, `no reply`, `refused`, and `already complained` jump to the
  correct next stage.
- Repeated emergency input does not reproduce the full emergency template.
- A resolved emergency resumes the preserved case.
- Fact collection never repeats the same question and still enforces its
  two-round limit.
- Separate-case notices are not duplicated.
- Terminal no-progress requests return `409` and create no turn.
- Existing historical sessions remain readable and participate in guarding.
- Trial, local full-test, and registered flows retain their ownership and
  quota behavior.

### Persistence and Concurrency Tests

- SQLite and PostgreSQL both reject an equivalent second write after the
  transactional recheck.
- Concurrent identical continuations can create at most one new turn.
- A rejected duplicate does not bind attachments or partially update session
  facts.
- A successful repaired response commits facts, response, usage, and
  attachments atomically.

### Frontend Tests

- A formal plan update displays its concise change reply before any full-plan
  details.
- The complete updated plan is collapsed by default.
- An unverified follow-up renders as a short progression reply, not another
  full guidance block.
- `case_no_progress` creates no duplicate chat bubble and leaves the composer
  usable.
- Restored old and new plan-update records both render correctly.
- Conversation numbering remains based on persisted turns.

## Acceptance Criteria

- No two accepted continuation turns in one consultation have equivalent
  visible-core content without a material fact, stage, action, question, case,
  or risk change.
- Adding a fact never causes the previous full answer to appear again as the
  new visible response.
- Repeated `continue` messages walk through unfinished work and stop creating
  turns when no progress is possible.
- No duplicate correction performs a second provider call.
- All focused and full automated test suites pass using the Fake Provider.
