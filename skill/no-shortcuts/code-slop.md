# Code slop checklist

From published research and community tools. Not failure-tested in this skill's scenarios, so use judgment. Each item is a question to ask of the diff.

## Size and shape
- Does each abstraction have two or more real uses? Remove one-implementation interfaces, single-method classes and wrappers that only delegate.
- Could this be fewer lines with the same behavior?
- Did the change stay inside what was asked?

## Reuse
- Does this duplicate something already in the repo? Search before adding a helper.
- Did you match the patterns and naming in neighboring files?

## Errors
- Is each try/catch handling a failure you can name and act on? Remove the ones that only log and continue.
- Do validation checks guard against input that can actually occur?

## Comments and names
- Does each comment explain why? Delete ones that restate the code.
- Do names say what the thing is? Replace `data`, `result`, `temp`, `handler2`, `userObject`.

## Honesty
- Does every imported package and called API exist in this version?
- Are debug prints, commented-out code and unused files gone?
