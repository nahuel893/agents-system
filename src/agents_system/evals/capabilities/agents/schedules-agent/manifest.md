---
role: schedules-agent
extends: platform/roles/agent
version: "1.0"
tools: [create_schedule, list_schedules, delete_schedule, escalation_notifier]
permissions:
  - write:schedule
  - send:escalation
---

# Manifest: schedules-agent

Fixture agent for the `schedules` capability (#175, ADR-011). It keeps the
recurring requests of the person it talks to with the three schedule tools.
It never chooses who receives a scheduled answer: the platform delivers only
to the person who asked.
