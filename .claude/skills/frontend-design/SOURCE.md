# Source

This folder is a vendored copy of the official `frontend-design` skill. `SKILL.md` has not been changed.

- Upstream: https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design/skills/frontend-design
- Pinned commit: `7779afb12e3635f46f56ec823979d68350ae000b`
- License: the upstream plugin README has no license note. The `SKILL.md` frontmatter says "Complete terms in LICENSE.txt", but that file is not in the upstream folder. The upstream repo `LICENSE.md` says: "© Anthropic PBC. All rights reserved. Use is subject to Anthropic's Commercial Terms of Service."

At this commit the upstream folder holds only `SKILL.md`. To update, set `SHA` to the new upstream commit and run this from the repo root:

```sh
SHA=<new-commit>; curl -fsSL "https://raw.githubusercontent.com/anthropics/claude-code/$SHA/plugins/frontend-design/skills/frontend-design/SKILL.md" -o .claude/skills/frontend-design/SKILL.md
```

Then update the pinned commit above. If the upstream folder gains more files, copy those too.
