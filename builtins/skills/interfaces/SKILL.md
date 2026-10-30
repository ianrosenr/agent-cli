---
name: interfaces
description: Rules for public interfaces, compatibility, schemas, APIs, and MCP adapters.
version: 2.0.0
---

# Interfaces

Public interfaces MUST have explicit contracts and preserve backward compatibility unless the specification authorizes a breaking change.

Breaking changes MUST define migration behavior.

Use established interoperable protocols and machine-readable schemas where supported.

Agent-facing interfaces SHOULD expose MCP when agent interoperability is a product requirement. MCP adapters MUST call the same domain/service layer as other interfaces and expose narrow typed capabilities.

Interface errors MUST be actionable without exposing sensitive data.
