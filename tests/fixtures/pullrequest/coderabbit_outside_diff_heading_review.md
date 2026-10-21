<!-- coderabbit-ai-generated:v1 -->

> [!CAUTION]
> Some comments are outside the diff and can’t be posted inline due to GitHub limitations.
>
> **⚠️ Outside diff range comments (1)**
>
> <details>
> <summary><strong>🟠 Major</strong> · Pass <code>null</code> when no credit account exists. · <code>page.tsx:70-77</code></summary><blockquote>
>
> `app/(client)/admin/organizations/page.tsx:70-77`
> **🎯 Functional Correctness** | **🟠 Major** | **⚡ Quick win**
>
> **Pass `null` when no credit account exists.**
>
> `account && {...}` produces `undefined` when the organization has no account. `CreditAccountActions.account` accepts `CreditAccountData | null`, so strict type checking rejects this path. Use `account ? {...} : null`.
>
> <details>
> <summary>🤖 Prompt for AI Agents</summary>
>
> ```
> Review comment at @app/(client)/admin/organizations/page.tsx around lines 70 -
> 77:
> Update the account prop in the organization rendering to use a ternary that
> returns the existing account object when present and null when absent,
> preserving the CreditAccountActions.account type contract.
> ```
>
> </details>
>
> <!-- cr-comment:v1:3e4480747733d32d80397a5a -->
>
> </blockquote></details>

---

<details>
<summary>🤖 Prompt to fix review comments</summary>

```
Outside diff comments:
Review comments at @app/(client)/admin/organizations/page.tsx:
- Around line 70-77: Update the account prop in the organization rendering to
use a ternary that returns the existing account object when present and null
when absent, preserving the CreditAccountActions.account type contract.
```

</details>
