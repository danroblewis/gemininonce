# Safety

These are rough guard rails, not a guarantee. You should still review what Gemini changed
(`git diff`).

- **The test command runs in a sandbox.** That command runs whatever code Gemini wrote. Inside the
  sandbox there's no internet access (localhost still works), files can only be written inside the
  project and temp folders, and credential locations (`~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains,
  Chrome/Firefox profiles, `~/.netrc`, …) can't be read. It uses `sandbox-exec` on macOS and `bwrap`
  on Linux if installed, with a warning when neither is available. If your tests need the internet,
  use `--no-sandbox`.
- **Every added line is checked before a file is written.** Only lines Gemini added are checked, not
  ones that were already there. The checks look for:
  - recursive deletes, disk wipes, destructive SQL or git
  - system paths, your home folder or credentials
  - `curl | sh`
  - webhooks and pastebins, raw-IP URLs
  - obfuscated `exec`
  - startup hooks (`.zshrc`, cron, LaunchAgents), `sudo`
  - `git push`, `npm publish`, sending email
  - crypto miners
  - hard-coded secrets

  It also flags writes to `.git/`, CI workflows, `.env` and key files, install hooks
  (`postinstall`, `cmdclass`), and a file shrinking by more than 80%. For Python it adds new
  findings from [Bandit](https://github.com/PyCQA/bandit), via `bandit` or `uvx bandit`. Serious
  findings need your `y`; otherwise the file is rejected and Gemini is told why. Minor ones (plain
  `subprocess`, network calls) are just shown.
- **New files need your OK.** Gemini sometimes makes up paths (e.g. `src/todo.py` when the code is
  in `todo/service.py`). A file that doesn't exist yet is only created if you type `y`. Otherwise
  Gemini is told which files really exist. Use `--allow-new-files` to skip the question.
- **Suggested shell commands get the same checks,** shown with warnings before you approve or
  modify them. Commands you approve run outside the sandbox, because they may need the internet
  (e.g. `pip install`).
- **Secrets aren't sent to Gemini.** Files that look like credentials (`.env`, `*.pem`, …) or that
  contain keys or tokens (AWS, GitHub, Google, Slack, OpenAI/Anthropic, Stripe, private keys, JWTs,
  `password = "..."`) are never sent. Anything that looks like a secret in test output is replaced
  with `[REDACTED]`.
