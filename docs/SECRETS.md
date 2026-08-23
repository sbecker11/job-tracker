# Secrets handling — workspace-wide

How credentials are stored, encrypted, and backed up across the three sibling
repos. Sibling `README.md` files link here instead of duplicating unlock steps.

Also see the umbrella [`README.md`](README.md) for the full new-machine checklist.

## git-crypt: `.env` is tracked, not git-ignored (2026-08-19)

`job-tracker`, `recruiting-automation`, and `comms-migration` are **public**
GitHub repos, but each tracks a real `.env`. That is safe because
`.gitattributes` routes `.env` through
[git-crypt](https://github.com/AGWA/git-crypt): GitHub only stores AES-256
ciphertext. On a machine with the key registered, the working-tree `.env` is
normal plaintext.

| Item | Location |
|------|----------|
| Per-repo key | `~/.git-crypt-keys/<reponame>.key` (`chmod 600`; dir `chmod 700`) |
| Shared Anthropic key (optional) | `workspace-recruiting-automation/.env` (not in any git repo) |
| OAuth client JSON / tokens | `~/.config/job-tracker/`, `~/.config/comms-classifier/` |

**If a key is lost with no backup, that repo’s `.env` history is unrecoverable —
there is no backdoor.**

### Decrypting `.env` on a new machine (canonical steps)

Same procedure for each sibling (`comms-migration`, `job-tracker`,
`recruiting-automation`):

1. Install git-crypt: `brew install git-crypt`
2. Copy the key **out of band** (scp / encrypted USB / password manager).  
   **Never** email it, commit it, or paste it into chat.
   ```bash
   mkdir -p ~/.git-crypt-keys && chmod 700 ~/.git-crypt-keys
   # place <reponame>.key here; chmod 600 ~/.git-crypt-keys/*
   ```
3. Clone the repo, then from its root:
   ```bash
   git-crypt unlock ~/.git-crypt-keys/<reponame>.key
   ```
4. Verify:
   ```bash
   git-crypt status    # shows filter wiring, not lock state
   head .env           # must be readable text, not binary garbage
   ```
5. If you never unlock: safe default — `.env` stays ciphertext; apps fail
   closed or fall back to the shared workspace `.env` / defaults (never a silent leak).

After unlock (or any recreate of `.env`), re-run:

```bash
./tm-exclude-env-files.sh
```

## Local backup strategy: Time Machine, with `.env` excluded

| What | Time Machine? | Why |
|------|---------------|-----|
| `~/.git-crypt-keys/*.key` | **Included** | Keys never go to git; need a durable local copy (prefer encrypted backup disk) |
| Each repo’s `.git/` | **Included** | Insurance for unpushed commits |
| Each `.env` (workspace + 3 repos) | **Excluded** | Plaintext secrets; encrypted copy already lives in git history |

Excluding `.env` from Time Machine does **not** mean you lose the only copy:
ciphertext is in git (once pushed), and real credentials can be re-issued from
Google / Anthropic consoles.

### Re-applying the exclusion: `tm-exclude-env-files.sh`

`tmutil addexclusion` is per path and does **not** survive delete+recreate
(`cp .env.example .env`, fresh unlock, etc.). Re-run anytime:

```bash
./tm-exclude-env-files.sh
```

Idempotent — prints `tmutil isexcluded` confirmation for each existing `.env`.
