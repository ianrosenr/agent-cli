---
name: creating-a-new-skill
description: Rules for creating compact, non-redundant skill files with valid metadata.
version: 1.0.0
policy_domain: skill-authoring
---

# Creating a New Skill

A skill MUST own one policy domain.

Every skill MUST begin with YAML front matter containing `name`, `description`, and `version`.

`name` MUST:

- be unique in the active skill set
- use lowercase kebab-case matching `^[a-z0-9]+(?:-[a-z0-9]+)*$`
- match the specialist filename as `<name>.md`; the canonical root `SKILL.md` is exempt

`description` MUST:

- be one plain-text line
- be at most 120 characters
- state only what policy the skill governs
- omit routing conditions, examples, implementation detail, and promotional language

`version` MUST use semantic versioning.

Metadata MUST contain no field without a defined CLI or policy consumer.

The skill body MUST:

- contain only rules needed when that skill is loaded
- avoid repeating universal rules from the root skill
- avoid repeating rules owned by another specialist skill
- reference the owning skill instead of restating cross-domain policy
- exclude routing logic; routing belongs in the CLI routing registry
- use normative language only when behavior is actually required

If a proposed rule has no single clear owner, resolve ownership before adding it.
