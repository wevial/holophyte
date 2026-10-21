# {{TITLE}}

<!-- A story is a directory: this body plus one ordinary ticket per child,
     each written from ticketTemplate.md with its own `## Story` role line.
     Once filed, the story's first line becomes `Story: KEY-n`. Each witness
     file lives beside this body under a `witnesses/` directory, at the path
     it will have in the repository. -->

## Summary

<Describe the outcome the whole story delivers in one or two sentences.>

## Goal

<The observable capability that exists once every witness passes, and for whom.>

## Witnesses

<!-- One line per witness, in the criterion form, numbered W1, W2, ...
     Drafting rules:
     1. A witness reaches only public surfaces: the CLI, the HTTP routes,
        the store API, the files the factory writes. Never a private helper.
     2. A witness asserts a settled outcome within a bounded number of
        passes, never the state after one pass.
     3. No witness is skipped: each one runs, and fails until its outcome
        holds.
     4. A witness covers a closed list of named cases, never "every" or
        "any" over an unstated set. -->

- [ ] W1: <OUTCOME> (a test in <FILE> witnesses <X>)

## Witness commands

<!-- One fenced block, one `W1: COMMAND` line per witness. Each command runs
     from the repository root with relative paths only and exits 0 once its
     witness holds. -->

```
W1: <exact runnable command>
```

## Witness cadence

<!-- Optional; drop the section to run every witness at every commit of
     main. One fenced block, at most one line per witness, in one of the
     forms `Wn: paths P [P ...]`, `Wn: every Nh` or
     `Wn: paths P [P ...]; every Nh`. A loop pass runs such a witness only
     when files under the repository-relative paths P (or its own witness
     file) changed since its last verdict, and only when that verdict is N
     hours old or more (N from 1 to 168); otherwise it carries the last
     verdict to the new commit. A witness with no line runs at every
     commit. For example:
         ```
         W1: paths lib/reader/; every 6h
         ```
-->

## Standing orders

- <A rule every child ticket keeps: a constraint, a surface not to touch, a convention to follow.>

## Out of scope

- <Adjacent work or future enhancement the story explicitly excludes.>

## Open questions

- None  <!-- must read exactly this before a child enters the pickable queue -->
