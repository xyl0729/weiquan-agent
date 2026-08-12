# Local Full-Test Identity Design

## Status

Approved by the user on 2026-08-13.

## Problem

The backend already grants `LocalPrincipal` access to persisted
consultations, follow-up turns, history, and attachments when
`deployment_mode=local`. The browser does not know that. It treats every
unauthenticated browser as an anonymous trial, calls `/api/trial/consult`,
does not retain a session ID, and hides all workspace controls.

This prevents the project owner from testing the registered-user workflow on
`http://127.0.0.1:8000`, even though the local backend supports it.

## Goals

- Give local development an explicit full-test identity without registration,
  email verification, or login.
- Let that identity create persisted consultations, continue the same
  consultation, use history, and upload and review attachments.
- Exempt local testing from anonymous and registered application quotas.
- Keep the existing real DeepSeek provider and disclose its actual model in
  each response.
- Keep local test data and identity separate from future accounts used through
  the production domain.
- Preserve all anonymous-trial and registered-account behavior outside local
  deployment mode.
- Show a precise message when public registration is closed.

## Non-Goals

- Do not add a model picker. Only one real public model is currently
  available: `deepseek-v4-flash`.
- Do not change the consultation pipeline's existing per-case follow-up and
  lifecycle rules. Local mode removes daily, monthly, and total call quotas;
  it does not create an unlimited-length single case.
- Do not change production registration, email, CAPTCHA, privacy, quota,
  provider, or rollout policy.
- Do not expose local mode through the production domain or open any public
  network listener.
- Do not deploy this change to the production server before the normal release
  process and public-launch prerequisites are satisfied.

## Decision

### Server-Authoritative Runtime Capability

Add `GET /api/runtime-config`, a small read-only runtime configuration
endpoint. Its response contains only a non-sensitive identity mode:

```json
{"identity_mode": "local_full_test"}
```

or:

```json
{"identity_mode": "account"}
```

The server derives this value exclusively from `Settings.deployment_mode`.
Only `local` may return `local_full_test`; `test` and `production` must return
`account`. The browser hostname is never used as an authority.

The endpoint must not return secrets, provider credentials, internal paths, or
other deployment configuration.

### Frontend Identity and Capabilities

Add `local` as a distinct frontend identity status. Keep these concepts
separate:

- `hasWorkspaceAccess`: true for `local` and `authenticated`.
- `hasRegisteredAccount`: true only for `authenticated`.
- `usesApplicationQuota`: false for `local`, true for trial and registered
  identities according to their existing quota shapes.

On startup, the browser reads runtime configuration before restoring identity:

1. For `local_full_test`, it activates the local identity directly.
2. It does not call `/api/auth/me`, `/api/trial/start`, or the CSRF endpoint.
3. It restores the remembered consultation and attachment drafts for the
   current browser origin, loads history, and enables workspace controls.
4. For `account`, it runs the existing account-then-trial restoration flow
   unchanged.

The local header state reads `本地完整测试` and does not pretend to be an
email account. Its account button is disabled and cannot open the login
dialog. The anonymous-trial strip is hidden, and the quota summary reads
`不计应用额度` with a tooltip that notes real model calls can still incur API
cost. Login, logout, email verification, and account quota UI remain available
only in account mode.

Workspace UI gates must include both `local` and `authenticated`. This applies
to:

- starting a persisted consultation;
- sending a follow-up with the current `session_id`;
- listing, opening, refreshing, and deleting history;
- uploading, restoring, reviewing, confirming, replacing, retrying, and
  deleting attachments;
- restoring the current consultation after a refresh;
- the registered-size message composer limit.

Account-only actions continue to check `authenticated`, not general workspace
access.

### Consultation Data Flow

Local first and follow-up turns use `/api/consult`, not
`/api/trial/consult`. The frontend sends the current `session_id` for a
follow-up and retains the returned ID for later turns and refresh recovery.

The existing `require_write_principal` and `require_read_principal`
dependencies resolve `LocalPrincipal` only in local deployment mode.
`LocalPrincipal.user_id` remains the fixed local development owner, so the
existing history and attachment ownership checks continue to apply.

The existing consultation endpoint creates a quota controller only for
`RegisteredPrincipal`. Local calls therefore consume neither the anonymous
five-call allowance nor registered daily/monthly allowances. Every successful
real DeepSeek request can still consume paid API balance, which the UI must not
describe as free or costless.

### Local and Production Isolation

The same physical computer may use both environments:

- `http://127.0.0.1:8000` receives local capability only while its backend is
  configured with `deployment_mode=local`.
- The future HTTPS production domain receives `account` capability because
  its backend uses `deployment_mode=production`.
- Browser cookies and `sessionStorage` are scoped by origin, so local session
  IDs do not become production account sessions.
- Local records use the local development owner and local datastore.
  Production records use verified account IDs and the production PostgreSQL
  database.

The local development server remains bound to loopback. This feature does not
authorize binding it to a LAN or public interface.

## Error Handling

- If runtime configuration cannot be loaded, fail closed: do not infer local
  access from the URL. Show a service error and keep workspace-only actions
  unavailable until retry or reload.
- If a local persisted session no longer exists, use the existing expired
  consultation handling and offer a new consultation.
- Existing attachment, provider, storage, and capacity errors retain their
  current safe messages and retry behavior.
- Add a frontend mapping for `public_registration_closed` that explicitly
  states that public registration is not open yet, instead of displaying the
  generic operation failure.
- Production must never silently fall back from account mode to local mode.

## Verification

### Backend and Contract Tests

- Runtime configuration returns `local_full_test` only for local mode.
- Test and production modes return `account`.
- The response schema rejects extra configuration fields.
- Local first-turn consultation persists and returns a session ID.
- A local follow-up using that ID appends to the same consultation.
- Local history list/detail/delete operations use the local owner.
- Local attachment upload/review/use operations remain ownership-checked.
- Local consultation responses do not contain a trial or registered quota.
- Production unauthenticated requests to workspace APIs still require
  registration.

### Frontend and Browser Tests

- Local startup does not create a trial identity and displays the local
  full-test state.
- Local first turn and follow-up both use `/api/consult`; the follow-up carries
  the same session ID.
- Local history and attachment controls are visible and functional.
- Refresh restores a local consultation from the current origin.
- Local mode has no anonymous or registered application quota blocker.
- Account mode retains the anonymous five-call flow.
- A verified registered account retains daily/monthly quotas and full
  workspace access.
- Production browser flows never display or activate the local identity.
- Closed registration displays the specific Chinese message.

Run focused API, frontend contract, and Playwright tests, then the full test
suite. Visually inspect the local page at desktop and mobile widths to confirm
that the identity label and workspace controls do not overlap existing header
or composer content.
