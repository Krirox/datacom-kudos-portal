# Kudos System Specification

**Status: Approved for implementation**

Product: internal employee portal, Kudos feature
Audience: authenticated employees, plus administrators who moderate the public feed

## Specification review

The starting request was: a signed-in person can choose a colleague, write a short note of appreciation, submit it, and see recent kudos on the main dashboard.

That request was enough to describe the happy path. It left the public feed unprotected. Anyone could post spam, a duplicate note, or an inappropriate message, and nothing in the design could take that message off the dashboard.

The review added four decisions before implementation:

1. An administrator can hide or delete a kudos message. Hide keeps the row and removes it from the public feed. Delete removes the row.
2. The `kudos` table has `is_visible` (integer boolean, default `1` / true). A new kudos is public unless a moderator hides it.
3. Moderation also stores `moderated_by`, `moderated_at`, and `reason_for_moderation` on the kudos row. Hide requires a reason.
4. Deletes still need an audit trail, so a `moderation_events` table keeps the actor, action, reason, and a snapshot of the message after the kudos row is gone.

Accounts are provisioned. There is no public sign-up. The demo database seeds four colleagues so the feature can be reviewed without a separate admin tool.

## Functional requirements

### User stories

1. As an employee, I can sign in with my work email and password, and I can sign out.
2. As an employee, I can select another employee from a list. I cannot select myself.
3. As an employee, I can write a message of appreciation of at most 500 characters.
4. As an employee, I can submit the kudos, and the application stores it.
5. As an employee, I can view a feed of recent visible kudos on the dashboard.
6. As an administrator, I can hide or delete inappropriate kudos messages, and I can restore a hidden message.

### Acceptance criteria

**Story 1. Sign in**

- Given a provisioned email and the correct password, sign-in starts a session and shows the dashboard.
- Given an unknown email or a wrong password, the response is the same generic failure. The client does not learn which part was wrong.
- Sign-out clears the session. Later requests to the feed or the form require sign-in again.
- A person who is not signed in cannot read the colleague list, post kudos, or moderate.

**Story 2. Choose a colleague**

- The list contains every other provisioned person, sorted by name, and omits the signed-in person.
- Submitting a recipient id that is not a real user is rejected.
- Submitting your own id is rejected.

**Story 3. Write the message**

- The message is required. A blank message, or a message that is only whitespace, is rejected.
- The stored message is trimmed. The length limit applies after trimming.
- A message longer than 500 characters is rejected and nothing is stored.
- The composer shows how many characters are left.

**Story 4. Submit**

- A valid submission stores the sender, the recipient, the message, and the time, and returns the new kudos.
- `is_visible` defaults to true. The sender does not choose visibility.
- The same sender, the same recipient, and the same message (ignoring capital letters) is rejected for 24 hours. A different message to the same person is allowed.
- A sender cannot store more than 20 kudos in a rolling hour. Hidden kudos still count toward that limit.
- Two identical requests at the same time cannot both succeed. The duplicate check and the insert run in one transaction.

**Story 5. Public feed**

- The dashboard lists visible kudos, newest first.
- Each item shows the sender's name, the recipient's name, the message, and the time.
- Hidden kudos are absent from this feed for every role, including the sender.
- The feed is paged. The default page size is 10. A client cannot request more than 50 items at once.
- An empty feed shows an empty state rather than an error.

**Story 6. Moderation**

- Only an administrator can list hidden kudos, hide, restore, or delete.
- An employee who calls a moderation route receives a permission error, and the row does not change.
- Hide sets `is_visible` to false and records the administrator, the time, and a reason of 1 to 200 characters. A missing reason is rejected.
- The hidden message disappears from the public feed and remains on the administrator list, marked hidden.
- Restore makes it visible again and clears the moderation fields on the row. Restoring a message that is already visible is rejected.
- Delete removes the kudos row. The public feed and the administrator list no longer show it.
- Every hide, restore, and delete writes a moderation event. A delete event includes a snapshot of the message.

### Edge cases

| Situation | Result |
| --- | --- |
| Spam burst from one account | The 21st kudos in an hour is rejected. Earlier rows stay stored. |
| Inappropriate message | An administrator hides it (with a reason) or deletes it. The public feed drops it immediately. |
| Duplicate submission | The second identical note to the same person inside 24 hours is rejected. |
| Message contains HTML or a script tag | It is stored as text and rendered as text. It is not interpreted as markup. |
| Unknown kudos id | Hide, restore, and delete report that the kudos was not found. |
| Page number is not a positive integer | The request is rejected. |

## Technical design

### Database schema

SQLite, with foreign keys enabled. Timestamps are ISO-8601 UTC text.

**users**

| Column | Type | Notes |
| --- | --- | --- |
| id | integer primary key | |
| name | text, required | Display name |
| email | text, required, unique | Sign-in name |
| password_hash | text, required | PBKDF2 hash, never returned by the API |
| role | text, required | `employee` or `admin` |
| created_at | text, required | |

**kudos**

| Column | Type | Notes |
| --- | --- | --- |
| id | integer primary key | |
| sender_id | integer, required | Foreign key to users. Cannot equal recipient_id |
| recipient_id | integer, required | Foreign key to users |
| message | text, required | 1 to 500 characters |
| created_at | text, required | |
| is_visible | integer, required, default 1 | Boolean. `1` is visible, `0` is hidden |
| moderated_by | integer, nullable | Foreign key to users. Set when hidden |
| moderated_at | text, nullable | Set when hidden |
| reason_for_moderation | text, nullable | Required by the application when hiding |

Indexes: `(is_visible, created_at, id)` for the feed, `(sender_id, created_at)` for the hourly limit, and `(sender_id, recipient_id, created_at)` for the duplicate check.

**moderation_events**

| Column | Type | Notes |
| --- | --- | --- |
| id | integer primary key | |
| kudos_id | integer, required | Not a foreign key, so the event survives deletion |
| action | text, required | `hide`, `restore`, or `delete` |
| actor_id | integer, required | Foreign key to users |
| reason | text, nullable | |
| message_snapshot | text, nullable | Copy of the message, always set for delete |
| created_at | text, required | |

### API endpoints

All bodies are JSON. Errors are `{"error": "<plain-language message>"}`. Mutating requests require `Content-Type: application/json`. The session cookie is `HttpOnly` and `SameSite=Lax`.

| Method and path | Who | Success | Failure |
| --- | --- | --- | --- |
| `POST /api/login` | public | 200 and the public user, sets the session cookie | 401 invalid credentials, 400 malformed body |
| `POST /api/logout` | any | 200, clears the cookie | |
| `GET /api/me` | signed in | 200 public user | 401 |
| `GET /api/users` | signed in | 200 array of `{id, name, email}` excluding the caller | 401 |
| `POST /api/kudos` | signed in | 201 kudos object. Body: `recipient_id`, `message` | 400 validation, 409 duplicate, 429 hourly limit |
| `GET /api/kudos?page=1&page_size=10` | signed in | 200 `{items, page, page_size, total}` of visible kudos | 400 bad paging |
| `GET /api/admin/kudos?page=1&page_size=10` | admin | 200 the same shape, including hidden rows | 403 employee |
| `POST /api/kudos/{id}/hide` | admin | 200 updated kudos. Body: `reason` | 400 bad reason, 403, 404 |
| `POST /api/kudos/{id}/restore` | admin | 200 updated kudos | 400 already visible, 403, 404 |
| `DELETE /api/kudos/{id}` | admin | 200 `{"ok": true}` | 403, 404 |

A kudos object contains `id`, `sender_id`, `sender_name`, `recipient_id`, `recipient_name`, `message`, `created_at`, `is_visible`, `moderated_by`, `moderated_by_name`, `moderated_at`, and `reason_for_moderation`.

`GET /` returns the dashboard page. CSS and script are static files from the same origin.

### Frontend components

One page, no framework.

- **Sign-in panel.** Email, password, and a demo hint. Shown when `GET /api/me` returns 401.
- **Portal header.** Product name, the signed-in person's name and role, and Sign out.
- **Composer.** Colleague `<select>`, message textarea, live character count, submit button, and an error or success region.
- **Public feed.** Paged list of visible kudos. Empty state when `total` is 0.
- **Moderation panel.** Rendered only when `role` is `admin`. Paged list of every kudos, a reason field, Hide, Restore, and Delete.

The layout is two columns from 800 pixels wide and one column below that. Controls are at least 44 pixels tall. Text is inserted with `textContent`, never `innerHTML`.

### Security

- Passwords are hashed with PBKDF2-HMAC-SHA256, a random salt, and 100,000 iterations. The API never returns the hash.
- Unknown users and wrong passwords share one error message. A missing user still runs a hash so the timing is closer.
- Session tokens are random, expire after 8 hours, and live in server memory. Restarting the process signs everyone out.
- Moderation routes check the role on the server. Hiding a button is not the control.
- SQL uses parameter binding. The message length, recipient, and reason are checked before the write.
- The page sends `Content-Security-Policy: default-src 'self'` and `X-Content-Type-Options: nosniff`.
- The cookie is marked `Secure` only when the deployment is HTTPS. The local demo uses HTTP.

### Performance

- The public feed never loads the whole table. It reads one page, using the visibility and time index.
- Page size is capped at 50.
- The duplicate and rate checks read the indexed rows for that sender, not the whole table.
- This design fits a single small internal site. It is not a multi-server deployment: sessions are in memory, and SQLite has one writer.

### Error handling and logging

- Expected failures raise a typed error with an HTTP status and a sentence a person can read. Unexpected failures return 500 with a generic sentence and a server-side stack trace.
- The log records sign-in failures (email only), created kudos ids, and moderation actions (actor id, kudos id, action). It does not record passwords or message text.
- Hide and delete also persist a row in `moderation_events`, which survives a process restart. In-memory logs do not.

## Implementation plan

| Order | Task | Depends on |
| --- | --- | --- |
| 1 | Create the SQLite schema, including `is_visible` and moderation columns | Approved spec |
| 2 | Provision users and password checks | Schema |
| 3 | Session sign-in and sign-out | Users |
| 4 | Colleague list and kudos creation, including duplicate and hourly limits | Sign-in, schema |
| 5 | Paged public feed of visible kudos | Creation |
| 6 | Admin list, hide, restore, delete, and moderation events | Feed, `is_visible` |
| 7 | Dashboard page: composer, feed, moderation panel, responsive layout | API |
| 8 | Unit tests for the rules above, plus one HTTP pass through sign-in, post, feed, hide, and delete | Tasks 2 to 7 |

### Testing strategy

- Service tests call the Python API with a temporary database and a controllable clock. They do not use the network.
- One HTTP test starts the server on an ephemeral port and walks sign-in, listing colleagues, posting, reading the public feed, hiding as an employee (forbidden), hiding as an admin, and deleting.
- A message containing `<script>` is asserted to come back as ordinary JSON text.
- The demo password hash in tests uses fewer PBKDF2 iterations so the suite stays fast. The application default remains 100,000.

### Deployment

- Run `python app.py` from the project directory with Python 3.11 or newer. There are no third-party packages.
- The process listens on `127.0.0.1:8000` unless `KUDOS_PORT` is set. The database file is `kudos.db` unless `KUDOS_DB` is set.
- The first launch seeds Alex Chen, Sam Patel, Jordan Lee (employees) and Riley Morgan (admin). The demo password for each account is `kudos-demo`.
- Deleting `kudos.db` resets the demo. There is no migration path; the schema is created once for an empty file.
