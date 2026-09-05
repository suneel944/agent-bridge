# Operations

## Install and upgrade

`make install` installs a package snapshot in uv's user executable directory.
If needed, run `uv tool update-shell` and open a new terminal. `make install-dev`
installs an editable checkout. `sudo env "PATH=$PATH" make install-system`
installs into `/usr/local/bin`, with its environment under `/opt/agent-bridge`.
Each user's runtime state remains private.

Before upgrading from 0.2, finish both sessions and run `agent-bridge down` using
the old installation. Preserve the entire state directory: it contains worktrees,
not just cache data. Install 0.3 and relaunch each lane. First startup imports mail
into a separate database and retains the original. Never run old and new versions
against the same state directory concurrently.

## Daily use

```sh
agent-bridge status
agent-bridge issue list
agent-bridge issue claim 42
agent-bridge issue offer 42 --to codex --summary "commit, checks, remaining work"
agent-bridge issue accept 42 --offer-id CURRENT_OFFER_ID
agent-bridge report --state ready --summary "Result" --evidence "Checks and results"
```

Mutations and reports run from the assigned lane. The owner pauses offered work
until acceptance, decline, or cancellation. Use `issue decline`, `issue cancel`,
and `issue release` explicitly; release does not close a GitHub issue. Partial or
blocked reports require `--remaining` instead of `--evidence`.

`up` starts the detached service; `down` stops its verified process and retains
state. Default state is `~/.local/state/agent-bridge`, mode 0700. Logs are in
`server.log`. Set `AGENT_BRIDGE_HOME` or pass `--home` for another private root.
Set `AGENT_BRIDGE_PORT` before first initialization to override port 8876.

Worktrees start at a captured commit and persist. Ignored environment files,
dependencies and untracked configuration are not copied. Set up each worktree
as needed. Review and integrate branches separately, then run the target repo's
combined verification gate.

## Plugins

Install the CLI first, then add the repository marketplace:

```sh
claude plugin marketplace add suneel944/agent-bridge
claude plugin install agent-bridge@agent-bridge-local
codex plugin marketplace add suneel944/agent-bridge
codex plugin add agent-bridge@agent-bridge-local
```

Both plugins provide `coordinate`; the launcher supplies MCP and native hooks.
Avoid installing the same skill from both personal and repo marketplaces. Start
a new native session after updates.

Repository installation does not imply public directory approval. For Claude,
validate `plugins/agent-bridge` with `claude plugin validate`, then use the
[community submission form](https://platform.claude.com/plugins/submit).
The official catalog is curated separately; see
[Claude's guide](https://code.claude.com/docs/en/plugins).

For Codex, follow [OpenAI's submission guide](https://developers.openai.com/plugins/deploy/submission).
This is a skills-only plugin. Submission requires a verified publisher, listing
and policy URLs, a skill bundle, and review cases. Neither catalog submission
has been made. CI builds artifacts; it does not submit review forms.

## Releases

Update package/plugin versions together and add a changelog entry. Run `make check`
and `make release-artifacts`. After review and merge, an annotated `vVERSION` tag
triggers release CI. It reruns the gate, creates a draft, uploads assets, downloads
and verifies their checksums, then publishes. Failed verification leaves a draft.

Download assets together and run `sha256sum --check SHA256SUMS`. The wheel needs
no third-party runtime packages. Development tools and the independent MCP test
client are locked in `uv.lock`.
