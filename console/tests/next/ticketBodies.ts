export const adjacentFencesBody = `# Adjacent blocks

## What / Why / How

**What:** Original outcome.
\`\`\`text
Keep this example.
\`\`\`

**Why:** Original reason.
   ~~~~text
Keep this explanation.
   ~~~~

**How:** Original prose.
\`\`\`sh
echo keep-me
\`\`\`
`;

export const detailedBody = `# Steer notes

Preamble stays here.

## Summary

Show the operator's notes.

## What / Why / How

**What:** Show notes on the run card.
Continue the same paragraph.

**Why:** Operators need context.

**How:** Read the stored notes.

<!-- Keep this implementation constraint. -->

## Reproduce

Open a run with saved notes.

## In scope

- Run card
- Stored notes

Scope annotation stays.

## Out of scope

- Notifications

## Acceptance criteria

- [ ] Saved notes appear.
- [ ] Empty notes stay hidden.
- [ ] Existing run actions still work.

> Reviewer rules: verify the visible behavior.
> Keep this quotation intact.

## Verify command(s)

Before commands stays.

\`\`\`sh
bun test
## This is a command comment, not a section
printf 'done'
\`\`\`

After commands stays.

\`\`\`
second fence stays
\`\`\`

## Contract checks

\`\`\`
console/settings.json: notes
## Also not a section
\`\`\`

## Implementation notes

Use the existing store.

## Estimate & dependencies

Estimate: 30 min · Depends on: HOLO-100, HOLO-101
Orchestration: subagents

## Open questions

- None
`;

export const windowsBody = "# Windows ticket\r\n\r\n## Summary\r\n\r\nKeep these endings.\r\n\r\n## Verify command(s)\r\n\r\n```\r\n## Inside the fence\r\nbun test\r\n```\r\n";

export const mockupBody = `# Card design

## What / Why / How

**What:** Show a card.

**Why:** Make notes visible.

**How:** Reuse the card.

**UI change:** major

## Mock-up

Keep this design note.

https://claude.ai/artifact/original

Approved 2026-10-10: card layout

![Overview](https://assets.example/overview.png)
![Detail](https://assets.example/detail.png)

Keep this export note.

## In scope

- Card
`;

export const fencedBody = `Intro without a title.

~~~~text
## Not a section
~~~
## Still inside the longer fence
~~~~

## Summary

A real section.

\`\`\`\`text
## Not a section either
\`\`\`
## Still inside four backticks
\`\`\`\`
`;

export const adjacentMarkdownBody = `## What / Why / How

**How:** Read the record.
**Keep** reading.
- Keep the existing database.
\`\`\`sh
echo keep-me
\`\`\`
`;

export const commentedCriteriaBody = `## Acceptance criteria

<!--
- [ ] Hidden editor note
\`\`\`
## Hidden heading
-->
- [ ] Visible criterion
<!-- - [ ] Another hidden note -->
`;

export const inlineCommentMarkerBody = `## Summary

Render the literal \`<!--\` in the editor.

## Acceptance criteria

- [ ] Keep the criterion.

## Estimate & dependencies

Estimate: 30 min · Depends on: none
`;

export const annotatedFieldsBody = `## What / Why / How

**How:** Read the stored notes. <!-- keep explanation -->

## Acceptance criteria

- [ ] Saved notes appear. <!-- keep note -->

> Keep reviewer rules.
`;

export const unclosedCommentBody = inlineCommentMarkerBody.replace("Render the literal `<!--` in the editor.", "Render the literal <!-- in the editor.");
export const multilineInlineCodeBody = inlineCommentMarkerBody.replace("Render the literal `<!--` in the editor.", "Show `the opening marker\nvalue <!--` literally.");

export const continuedHowBody = `## What / Why / How

**How:** Read the record.\u0020\u0020
Continue reading.

## In scope

- Reader
`;

export const closedCommentsBody = `## Summary

Show \`<!-- hidden -->\` literally. <!-- first <!-- second --> Visible tail.

## Verify command(s)

\`\`\`
printf 'start'<!-- hidden
still hidden -->
printf 'done'
\`\`\`

## Open questions

- None
`;

export const labeledCommentBody = `## What / Why / How

**How:**<!-- keep explanation --> Read the record.
`;

export const adjacentHtmlBody = `## What / Why / How

**What:** Show the record.
<div>Keep this warning.</div>

**Why:** Explain the record.
<div>Keep this warning.</div>

**How:** Read the record.
<div>Keep this warning.</div>
`;

export const inlineHtmlBody = `## What / Why / How

**How:** Read the record.
<span>Keep reading.</span>
<https://example.com/record>
`;

export const literalVerifyBody = `## Verify command(s)

<!-- Keep the guidance. -->

\`\`\`sh
printf "<!-- keep -->"
\`\`\`

## Open questions

- None
`;
