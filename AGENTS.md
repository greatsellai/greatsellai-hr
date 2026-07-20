# GreatSell AI HR Agent Working Rules

## GitHub sync comes first

Before **every** investigation, implementation, review, test, or deployment-related task, check the GitHub baseline before touching source files:

```bash
git fetch --prune --tags origin
git status --short
git rev-parse HEAD
git rev-parse origin/main
git log --oneline --decorate HEAD..origin/main
```

State whether the current checkout is already based on the latest `origin/main`.

- For a new task, create a fresh branch from the updated baseline:

  ```bash
  git switch main
  git pull --ff-only origin main
  git fetch --tags origin
  git switch -c <type>/<short-task-name>
  ```

- For an existing feature branch, inspect its working tree and compare it with
  `origin/main` before continuing. Do not blindly reset, force-push, or rebase
  a dirty branch. Rebase or merge only after checking the diff and resolving
  conflicts deliberately.
- If GitHub has newer commits that materially affect the task, update the
  branch before implementation and mention the new baseline in the progress
  update.

`origin/main` is the only code baseline. A server is a deployment target, not
a source of truth.

## Delivery workflow

1. Read the applicable product and implementation documents before making a
   material design decision.
2. Work in an independent feature branch and worktree. Do not edit another
   contributor's working directory.
3. After each verifiable milestone, run `git diff --check`, inspect the diff,
   commit, and push the feature branch.
4. Before opening a pull request, run the relevant backend tests and
   `npm run build` for the web application.
5. Open a PR to `main`; do not push directly to `main`.
6. Report the PR link, branch, commit, tests, migration implications, and any
   remaining production configuration steps.

## Production and privacy guardrails

- Do not edit business code directly on a server.
- Do not change DNS, Caddy, Compose, deployment scripts, or production
  environment files unless the user explicitly authorizes that operation.
- Never commit environment files, database dumps, uploads, PDFs, candidate
  data, API keys, passwords, tokens, SSH keys, or deployment artifacts.
- Treat candidate data as workspace-scoped. Every API, worker task, mailbox
  import, AI operation, and original-file access must use the authenticated
  workspace context.
- AI output must remain evidence-grounded and advisory. It must not
  automatically reject or hire a candidate.

## Release checks

Before declaring a release ready, verify the relevant paths rather than only
the happy path: registration/login, workspace isolation, upload/extraction,
filtering, scoring, JD matching, original-file access, mailbox behavior, and
failure/retry handling. Include rollback and migration notes whenever a
database or durable upload volume is affected.
