---
name: no-shortcuts
description: Use when writing, editing or fixing code or building UI, especially under time pressure, when a test or CI check is failing, when something is blocked or unreachable, or when the user cannot be reached.
---

# No Shortcuts

**Core principle:** a check going green must mean the thing works. An honest red beats a faked green.

## The rule tested under pressure

Never change a test, CI script or check so it reports success while the behavior stays unverified. That includes skips, conditional skips, mocks standing in for the thing under test, and loosened assertions.

"Get it passing" is not permission to weaken a check. Only the user naming that exact change is.

**When a check cannot honestly pass:**

1. Leave the check as it is.
2. Fix what you can, then state the root cause and exactly what is needed.
3. If the user is waiting on a bypass, write it to `bypass.patch` without applying it, and give them the one command to apply it. The choice stays theirs.

| Excuse | Reality |
|---|---|
| "You can't be reached, so I had to decide" | Being unreachable leaves the decision open. Prepare the patch and report. |
| "It's unrelated to this change" | You can't verify that from here, and the user can't see what stopped being checked. |
| "Someone is down right now" | Urgency justifies speed, and it never justifies a false signal. |
| "I disclosed it, so it's fine" | Disclosure in a summary doesn't undo a green badge. Others only see the badge. |
| "It only skips when unreachable" | A narrow skip is still a skip: nothing was verified. |

## Before writing (research-based, not pressure-tested)

1. Read the code around the change and search for existing helpers.
2. Confirm every API, flag and package exists.
3. Fake or sample data is allowed only when visibly labeled in the output and named in your report.

## When the scanner hook blocks you

Fix the cause. Add `no-shortcuts-ok: <reason>` to a line only when the code is truly correct as written. The reason needs at least two words, and the user sees every waiver. For a truly intended test deletion, add a comment in that test file with `no-shortcuts-deleted-ok: <reason>`.

## Finish report

Run the check before you claim the result. End with these four parts:

- **Done:** what now works
- **Verified by:** the command or check, and its result
- **Not done:** anything skipped, stubbed, guessed or untested
- **Deviations:** where you departed from the request

## Slop checklists

Read during self-review. Both are research-based guidance, not pressure-tested rules.

- Any code: `code-slop.md`
- Pages, screens, UI components: `ui-slop.md`
