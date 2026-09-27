# Accounts, Chrome profiles and anonymous mode

## Which account is used

**Use your own Chrome login (SSO, 2FA, work account):**

```sh
gemininonce --chrome-profile list x              # show your Chrome profiles
export GEMININONCE_CHROME_PROFILE="Profile 2"     # dir, name, or email
export GEMININONCE_ACCOUNT=@yourcompany.com
```

Chrome 136 and later refuse to let automation control your real Chrome data directory. So at the
start of each run your profile is copied, without caches or history, into `~/.gemininonce/chrome`,
which only your user can read. Chrome then runs from that copy. The first copy is a few hundred MB,
and later runs copy only what changed. On macOS the cookies decrypt through the Keychain, so you're
already logged in. Your normal Chrome can stay open.

The copy trick works on macOS and Linux. On Windows, Chrome's app-bound cookie encryption means a
copied profile won't be logged in.

**Without `--chrome-profile`**, the tool uses its own empty profile (`~/.gemininonce/profile`),
where you'd sign in once.

**Account check:** Gemini lets you chat while signed out, and nothing would look wrong. So before
every message the tool reads the signed-in account from the page, and it refuses to send if
Gemini is signed out or doesn't match `GEMININONCE_ACCOUNT` / `--account`. If several Google
accounts are signed in, it switches to the matching one.

## Anonymous mode (free tier)

```sh
gemininonce src/ -t "pytest -x" --anonymous
```

`--anonymous` guarantees a signed-out, free-tier session:
- **A fresh throwaway browser profile** each run. No Google account, no cookies, nothing copied
  from your Chrome. The profile is deleted afterwards.
- **It won't combine with account options.** `--chrome-profile`, `--account`, `--cdp` and
  `--profile` given on the command line are errors, and the matching environment variables are
  ignored.
- **Signed-out check:** before every message it checks that Gemini is signed out, and refuses to
  send if it isn't.
- **Model:** signed out, Gemini only offers **Flash-Lite**, so that's the default here. Asking for
  another model is an error, not a quiet switch.
- **Confirmation:** before anything is sent, it lists the files and warns that free-tier chats may
  be kept by Google, used to improve its products and read by human reviewers, so treat everything
  as public. That includes the list of file names in the project. If Gemini asks for any file you
  didn't list, you're asked before it's sent.
  You have to type `yes`. For scripts, `-y` skips the question; without a terminal and without
  `-y` it aborts.
