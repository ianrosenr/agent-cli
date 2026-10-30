---
name: creating-a-new-skill
description: Rules for creating compact, non-redundant skill packages with valid metadata.
version: 2.0.0
---

# Creating a New Skill

A skill MUST own one policy domain.

Each skill MUST be a directory named for the skill containing a root `SKILL.md`.

Every `SKILL.md` MUST begin with YAML front matter containing exactly `name`, `description`, and `version`.

`name` MUST:

- be unique in the active skill set
- use lowercase kebab-case matching `^[a-z0-9]+(?:-[a-z0-9]+)*$`
- match its containing directory

`description` MUST:

- be one plain-text line
- be at most 120 characters
- state only what policy the skill governs
- omit routing conditions, examples, implementation detail, and promotional language

`version` MUST use semantic versioning.

The skill body MUST:

- contain only rules needed when that skill is loaded
- avoid repeating rules owned by another skill
- reference the owning skill instead of restating cross-domain policy
- exclude routing logic; routing belongs in the CLI routing registry
- use normative language only when behavior is actually required

If a proposed rule has no single clear owner, resolve ownership before adding it.
