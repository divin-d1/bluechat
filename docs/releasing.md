# TestPyPI preview and release process

BlueChat's `0.1.0` package is a preview for installation and feedback. The
TestPyPI distribution is called `bluechat-terminal` because the unrelated
`bluechat` project name is already taken on TestPyPI. The installed CLI and
import package remain `bluechat`. The public PyPI `bluechat` name was
unclaimed when checked on 2026-10-01, but verify it again before a future
stable release. This is not a stable security release. Physical Bluetooth
interoperability remains unverified; see the
[hardware test matrix](testing/bluetooth-hardware.md).

## Install the TestPyPI preview

Once `0.1.0` has been uploaded to TestPyPI, testers can install it with:

```sh
python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  bluechat-terminal==0.1.0
bluechat --help
bluechat doctor
```

The extra index is needed because TestPyPI does not mirror the runtime
dependencies. TestPyPI is a separate package index from PyPI; uploading here
does not publish a package for ordinary `pip install bluechat` installs.

## Validate an artifact before upload

From the repository root:

```sh
python -m pip install -e ".[dev]"
pytest -q
ruff check .
mypy src/bluechat
python -m build
python -m twine check dist/*
```

Upload the reviewed wheel and source archive to TestPyPI with an account token.
Use the configured `testpypi` repository in `~/.pypirc`, or set
`TWINE_USERNAME=__token__` and `TWINE_PASSWORD` in the environment. Never put a
token in the repository, command history, README, or issue tracker.

```sh
python -m twine upload --repository testpypi dist/bluechat_terminal-0.1.0*
```

Verify the uploaded files from a clean virtual environment with the install
command above. PyPI indexes do not allow replacing an uploaded file with the
same project version; make a new version for any post-upload changes.

## GitHub source publication

The canonical source repository is `https://github.com/divin-d1/bluechat`.
Review the staged file list and diff before the first commit. Do not commit
virtual environments, caches, build output, downloaded dependency wheels, or
package-index credentials. The repository CI workflow runs tests, lint, type
checking, package builds, and CLI smoke checks on Linux, Windows, and macOS.

After pushing, check the Actions page and update the hardware matrix only with
results from real computers. Do not describe mocked or local checks as
physical Bluetooth interoperability tests.

## Feedback to request from preview testers

Please include the BlueChat version, OS version, adapter model, the exact
command or workflow, and sanitized logs when reporting issues. Never attach
room codes, private messages, downloaded personal files, or security tokens.
For vulnerabilities, follow [SECURITY.md](../SECURITY.md) and contact the
maintainers privately.
