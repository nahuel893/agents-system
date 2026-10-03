---
role: stock-bot
extends: platform/roles/agent
version: "1.0"
tools: [catalog_search]
permissions:
  - read:catalog
---

# Manifest: stock-bot

One tool, `catalog_search`, behind `read:catalog`.
