# Accounts, Chrome profiles and anonymous mode

## Which account is used

**Use your own Chrome login (SSO, 2FA, work account):**

```sh
geminonce --chrome-profile list x              # show your Chrome profiles
export GEMINONCE_CHROME_PROFILE="Profile 2"     # dir, name, or email
export GEMINONCE_ACCOUNT=@yourcompany.com
```

Chrome 136 and later refuse to let automation control your real Chrome data directory. So at the
start of each run your profile is copied, without caches or history, into `~/.geminonce/chrome`,
which only your user can read. Chrome then runs from that copy. The first copy is a few hundred MB,
and later runs copy only what changed. On macOS the cookies decrypt through the Keychain, so you're
already logged in. Your normal Chrome can stay open.

The copy trick works on macOS and Linux. On Windows, Chrome's app-bound cookie encryption means a
copied profile won't be logged in.

**Or sign in once in geminonce's own browser:** `--profile` uses its own profile (`~/.geminonce/profile`,
or `--profile DIR`). You sign in there the first time, and the login is remembered.

**With none of these** (no `--chrome-profile`, `--account`, `--profile` or `--cdp`, and no
`GEMINONCE_CHROME_PROFILE` or `GEMINONCE_ACCOUNT`), geminonce runs **signed out**. See below.

**Account check:** Gemini lets you chat while signed out, and nothing would look wrong. So before
every message the tool reads the signed-in account from the page, and it refuses to send if
Gemini is signed out or doesn't match `GEMINONCE_ACCOUNT` / `--account`. If several Google
accounts are signed in, it switches to the matching one.

## Anonymous mode (free tier): the default

```sh
geminonce src/ -t "pytest -x"            # no account given: signed out
geminonce src/ -t "pytest -x" --anonymous # force it, even if an account is set in your environment
```

Without an account, geminonce uses a signed-out, free-tier session:
- **A fresh throwaway browser profile** each run. No Google account, no cookies, nothing copied
  from your Chrome. The profile is deleted afterwards.
- **Forcing it:** with `--anonymous`, account options given on the command line (`--chrome-profile`,
  `--account`, `--cdp`, `--profile`) are errors, and the matching environment variables are ignored.
- **Signed-out check:** before every message it checks that Gemini is signed out, and refuses to
  send if it isn't.
- **Model:** signed out, Gemini only offers **Flash-Lite**, so that's the default here. Asking for
  another model is an error, not a quiet switch.
- **Confirmation, once:** the first time, before anything is sent, it lists the files and warns that
  free-tier chats may be kept by Google, used to improve its products and read by human reviewers,
  so treat everything as public. That includes the list of file names in the project. You type
  `yes` once, and it's remembered (`~/.geminonce/anonymous-ok`). After that, every run starts with a
  one-line notice that it's signed out. If Gemini asks for a file you didn't list, you're still
  asked before it's sent. For scripts, `-y` skips the question without remembering it; without a
  terminal and without `-y`, the first run aborts.
