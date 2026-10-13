**Actionable comments posted: 0**

> [!CAUTION]
> Some comments are outside the diff and can't be posted inline due to platform limitations.
>
>
>
> <details>
> <summary>⚠️ Outside diff range comments (1)</summary><blockquote>
>
> <details>
> <summary>src/app.py (1)</summary><blockquote>
>
> `40-52`: _⚠️ Potential issue_ | _🟠 Major_
>
> **Retry loop never resets its backoff after a success.**
>
> After one failed call the delay keeps doubling for the life of the process.
>
> <details>
> <summary>🤖 Prompt for AI Agents</summary>
>
> ```
> In src/app.py around lines 40 to 52, reset the delay to its initial value after a successful call.
> ```
>
> </details>
>
> </blockquote></details>
>
> </blockquote></details>

<details>
<summary>🧹 Nitpick comments (1)</summary><blockquote>

<details>
<summary>src/app.py (1)</summary><blockquote>

`12-14`: _🔵 Trivial_

**Prefer a named constant for the retry cap.**

The literal 5 appears twice; one name keeps the two in step.

</blockquote></details>

</blockquote></details>
