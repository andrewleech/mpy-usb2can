# MicroPython CLAUDE.md
This file provides context for AI coding agents working on the MicroPython codebase.

## Build Commands

Build the cross-compiler:
```bash
make -C mpy-cross
```

Build a specific port (e.g., unix):
```bash
make -C ports/unix
```

Build and run tests for a port (e.g., unix):
```bash
cd ports/unix
make test
```

Run a specific test script:
```bash
cd tests
./run-tests.py <test_script.py>
```

## Unit Tests
The unit tests are in tests/<category> folders.
They are generally written as python scripts that are run under both 
micropython a (c)python with the print outputs compared for consistency.
For tests that can only run on micropyhon a unittest based test is preferred else
 a <test name>.py script is accompanied by a <test name>.py.exp where the .exp file 
contains the expected print outputs to compare the test output against. 

## Linting and Formatting

Run lint and formatting checks:
```bash
pre-commit run --files [files...]
```

Format C code:
```bash
tools/codeformat.py [files...]
```
Requires uncrustify v0.71 or v0.72.

Format Python code:
```bash
ruff format [files...]
```

Check for spelling errors:
```bash
codespell
```

Use pre-commit hooks for automatic checks (recommended):
```bash
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Code Style Guidelines

**General:**
* Follow conventions in existing code.
* See `CODECONVENTIONS.md` for detailed C and Python style guides.

**Python:**
* Follow PEP 8.
* Use `ruff format` for auto-formatting (line length 99).
* Naming: `module_name`, `ClassName`, `function_name`, `CONSTANT_NAME`.

**C:**
* Use `tools/codeformat.py` for auto-formatting.
* Naming: `underscore_case`, `CAPS_WITH_UNDERSCORE` for enums/macros, `type_name_t`.
* Memory allocation: Use `m_new`, `m_renew`, `m_del`.
* Integer types: Use `mp_int_t`, `mp_uint_t` for general integers, `size_t` for sizes.

**Git Commits:**
* Prefix commit messages with the affected directory/file (e.g., `py/objstr: ...`). End sentences with `.`.
* First line max 72 chars, subsequent lines max 75 chars.
* Sign-off commits using `git commit -s`.


## Pull Requests
* The upstream repo https://github.com/micropython/micropython should always be used for PR's
* Git pushes should always go to the user's fork origin/remote
* The `gh` tool can be used to directly interact with Pull Requests:
  * List PRs: `gh pr list`
  * View PR details: `gh pr view <PR_NUMBER>`
  * View PR comments: `gh pr view <PR_NUMBER> --comments`
  * View PR diff: `gh pr diff <PR_NUMBER>`
  * Check out a PR: `gh pr checkout <PR_NUMBER>`
* When writing PR descriptions use the template:
``` markdown
### Summary
<!-- Explain the reason for making this change. What problem does the pull request
     solve, or what improvement does it add? Add links if relevant. -->

### Testing
<!-- Explain what testing you did, and on which boards/ports. If there are
     boards or ports that you couldn't test, please mention this here as well.
     If you leave this empty then your Pull Request may be closed. -->

### Trade-offs and Alternatives
<!-- If the Pull Request has some negative impact (i.e. increased code size)
     then please explain why you think the trade-off improvement is worth it.
     If you can think of alternative ways to do this, please explain that here too.
     Delete this heading if not relevant (i.e. small fixes) -->
```

### GitHub API with gh
The `gh` tool can also be used to directly access the GitHub API:

```bash
# Get review comments on a PR (these appear inline on code)
gh api -H "Accept: application/vnd.github+json" \
       -H "X-GitHub-Api-Version: 2022-11-28" \
       /repos/micropython/micropython/pulls/<PR_NUMBER>/comments

# Get PR issue comments (these appear in the main discussion)
gh api -H "Accept: application/vnd.github+json" \
       -H "X-GitHub-Api-Version: 2022-11-28" \
       /repos/micropython/micropython/issues/<PR_NUMBER>/comments

# Get PR review status
gh api -H "Accept: application/vnd.github+json" \
       -H "X-GitHub-Api-Version: 2022-11-28" \
       /repos/micropython/micropython/pulls/<PR_NUMBER>/reviews
```
The API responses are in JSON format and include detailed information about the comments, reviews, and PR status.
