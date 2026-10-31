# Changelog fragments

Add one file for each user-facing change. Name it
`<issue-number>.<kind>.md`, where kind is `added`, `changed`, `fixed`,
`removed`, or `security`. Write one or two STE lines in the file. Do not edit
`CHANGELOG.md` in a pull request.

Example: `123.fixed.md`. Use one fragment per issue and add more lines to it.

At release time, run `python scripts/collect_changelog.py 1.2.0 2026-10-29`.
The script groups all fragments by kind under the version header in
`CHANGELOG.md`, then removes the fragment files. Run
`python scripts/collect_changelog.py --check` to validate fragment names.
