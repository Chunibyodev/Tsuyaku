# Contributing to Tsuyaku

Thanks for helping! Bug reports, translation-quality examples, ideas and pull requests are all
welcome. Questions that don't fit an issue: contact@chunibyo.dev.

## Reporting a bug

Use the [bug report form](https://github.com/Chunibyodev/tsuyaku/issues/new/choose). The most
useful things to include:

* what the popup shows (the Speech and Translator lines, and *This tab*);
* the output of `uv run tsuyaku doctor` (in the Tsuyaku folder);
* the end of `tsuyaku.log` and `llama-server.log` (`doctor` prints the logs folder).

Bad translations are worth reporting too: the Japanese, what Tsuyaku made of it, the model you use
(settings page) and what it should have said.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and, for the add-on's tests, [Node.js](https://nodejs.org/) 20+.

```
uv sync                                 # add --extra cuda on an NVIDIA machine
uv run pytest                           # unit tests: no network, models or GPU needed
uv run ruff check src tests scripts     # lint
cd tests/js && npm ci && npm test       # the add-on's chat scripts in a simulated YouTube chat (jsdom)
npx web-ext lint --source-dir src/tsuyaku/extension   # Mozilla's add-on linter
```

To try the add-on, run `uv run tsuyaku setup` and `uv run tsuyaku firefox`, then load
`src/tsuyaku/extension/manifest.json` as a temporary add-on (`about:debugging`). After changing the
add-on, reload it there and reload the YouTube tab. After changing the Python side, turn Tsuyaku
off and on again.

`python scripts/fake_live_hls.py some.mp4` serves a local file as a fake live stream for
`tsuyaku run`.

## Where things are

* `src/tsuyaku/extension/`: the Firefox add-on (background page, popup, settings page, content
  scripts for the watch page and the chat).
* `src/tsuyaku/host/`: the native messaging host the add-on starts.
* `src/tsuyaku/asr/`, `src/tsuyaku/mt/`: speech recognition and translation.
* [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) explains the pipeline and the design decisions.

## Pull requests

* Keep them focused; one change per pull request is easiest to review.
* Add or update tests for behaviour changes; CI runs the commands above on Windows and Linux.
* Match the surrounding code style (ruff's settings are in `pyproject.toml`).
* By contributing you agree that your contribution is licensed under the
  [Apache License 2.0](LICENSE), like the rest of the project.

## Releasing

Set the new version in `pyproject.toml`, `src/tsuyaku/__init__.py` and
`src/tsuyaku/extension/manifest.json`, add a section for it to `CHANGELOG.md`, and push to `main`.
The Release workflow sees a version that has no release yet, tags it, signs the Firefox add-on
with Mozilla (repository secrets `JWT_ISSUER` / `JWT_SECRET`) and publishes the release.
