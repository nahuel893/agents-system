# Changelog

## [0.5.0](https://github.com/nahuel893/agents-system/compare/v0.4.0...v0.5.0) (2026-09-29)


### ⚠ BREAKING CHANGES

* **checkpointer:** the served app keeps conversations in PostgreSQL and refuses to boot until alembic upgrade head has created the checkpoint tables (revision 007); the server extra no longer installs langgraph-checkpoint-redis, and threads held in Redis are not migrated.
* a plain install no longer brings the server, database, SQL, embeddings or provider packages. Install agents-system[all], or the extras you need, to get them back.

### Features

* **agent:** trim the model input to a per-agent context budget ([#28](https://github.com/nahuel893/agents-system/issues/28)) ([#194](https://github.com/nahuel893/agents-system/issues/194)) ([c960704](https://github.com/nahuel893/agents-system/commit/c960704e67f10e0060401aad04674be808bfd634))
* **checkpointer:** keep conversations durable in Postgres with a separate retention ([#24](https://github.com/nahuel893/agents-system/issues/24)) ([#197](https://github.com/nahuel893/agents-system/issues/197)) ([b448c70](https://github.com/nahuel893/agents-system/commit/b448c703e044db2e9b63fa316a4e47a2d1f7f134))
* make a plain install the light core, with the rest as extras ([#188](https://github.com/nahuel893/agents-system/issues/188)) ([#196](https://github.com/nahuel893/agents-system/issues/196)) ([d4a2509](https://github.com/nahuel893/agents-system/commit/d4a2509d3580acbea140bec6d8ff59d17622d196))
* **runtime:** build the model an Agent declares in process ([#186](https://github.com/nahuel893/agents-system/issues/186)) ([#192](https://github.com/nahuel893/agents-system/issues/192)) ([c0cd0c8](https://github.com/nahuel893/agents-system/commit/c0cd0c813b26b6fa4edc86aa883a08aca69ecb87))


### Bug Fixes

* **agent:** repair unanswered tool calls in checkpointed threads after a mid-tool timeout ([#181](https://github.com/nahuel893/agents-system/issues/181)) ([#190](https://github.com/nahuel893/agents-system/issues/190)) ([3bce681](https://github.com/nahuel893/agents-system/commit/3bce6811ffbb75c92e92daca73d9e6134002e1a2))
* close the stage 0-1 review follow-ups ([#182](https://github.com/nahuel893/agents-system/issues/182)) ([#193](https://github.com/nahuel893/agents-system/issues/193)) ([09ecfff](https://github.com/nahuel893/agents-system/commit/09ecfffd32e151810bb973c78bbbf1e5a5f1dc91))
* **roles:** escalate on failed developer tasks, stop data-agent loops, gemini reference model ([#157](https://github.com/nahuel893/agents-system/issues/157)) ([#184](https://github.com/nahuel893/agents-system/issues/184)) ([7620c1f](https://github.com/nahuel893/agents-system/commit/7620c1fa2b34b87ad2f463ff8b3adbe6fed42f1c))


### Documentation

* add a feature reference page ([#200](https://github.com/nahuel893/agents-system/issues/200)) ([#201](https://github.com/nahuel893/agents-system/issues/201)) ([4ca19c9](https://github.com/nahuel893/agents-system/commit/4ca19c988ad1f267d8e46fa6aeeeb2fc549f4a2e))
* present agents-system as a development kit for building agents ([#187](https://github.com/nahuel893/agents-system/issues/187)) ([#195](https://github.com/nahuel893/agents-system/issues/195)) ([147c15c](https://github.com/nahuel893/agents-system/commit/147c15c5e087840da91d36fc6227e725cb1e6c38))

## [0.4.0](https://github.com/nahuel893/agents-system/compare/v0.3.1...v0.4.0) (2026-09-28)


### Features

* **channels:** extract a channel port behind the webhook route and worker ([#163](https://github.com/nahuel893/agents-system/issues/163)) ([027a898](https://github.com/nahuel893/agents-system/commit/027a89892e6393a6a5928612c6f1249d97018e58)), refs [#22](https://github.com/nahuel893/agents-system/issues/22)
* **harness:** run a one-tool agent in about ten lines with a built-in escalation default ([#168](https://github.com/nahuel893/agents-system/issues/168)) ([#178](https://github.com/nahuel893/agents-system/issues/178)) ([01e72da](https://github.com/nahuel893/agents-system/commit/01e72da395f03c0ff6e9643941e8d662f3b64563))


### Bug Fixes

* close the sixth-batch review follow-ups and reconcile ADR-002 status ([#165](https://github.com/nahuel893/agents-system/issues/165)) ([#180](https://github.com/nahuel893/agents-system/issues/180)) ([90aec40](https://github.com/nahuel893/agents-system/commit/90aec40085a40fef478a9eb885de5b7d6e9562a5)), refs [#177](https://github.com/nahuel893/agents-system/issues/177)
* **evals:** build the live-eval model once per test ([#155](https://github.com/nahuel893/agents-system/issues/155)) ([eb2af93](https://github.com/nahuel893/agents-system/commit/eb2af93820aff9bf55fa5515c450bff09983173e))
* **evals:** make the seven *_boundary scenarios exercisable ([#162](https://github.com/nahuel893/agents-system/issues/162)) ([68f360f](https://github.com/nahuel893/agents-system/commit/68f360fc23a1051110d9dc557c805a0cff978c94)), refs [#156](https://github.com/nahuel893/agents-system/issues/156)
* **library:** run turns in-process without the server environment ([#167](https://github.com/nahuel893/agents-system/issues/167)) ([4387aec](https://github.com/nahuel893/agents-system/commit/4387aec5ff029d4c4fb49934f5d7191fc0493586)), refs [#166](https://github.com/nahuel893/agents-system/issues/166)
* **release:** correct the uv.lock jsonpath so release-please bumps it ([#164](https://github.com/nahuel893/agents-system/issues/164)) ([0a9bf19](https://github.com/nahuel893/agents-system/commit/0a9bf193b9abb8dabb66c1ef7769e1c500ca1e0c)), refs [#150](https://github.com/nahuel893/agents-system/issues/150)
* **runtime:** report a timed-out turn as a timeout and keep its usage ([#172](https://github.com/nahuel893/agents-system/issues/172)) ([#179](https://github.com/nahuel893/agents-system/issues/179)) ([be63151](https://github.com/nahuel893/agents-system/commit/be63151661d90f783b2ecf12e304c1a12469247a))
* **tests:** isolate the offline suite from .env and ambient env vars ([#161](https://github.com/nahuel893/agents-system/issues/161)) ([108467c](https://github.com/nahuel893/agents-system/commit/108467cae49a27d79a7ff74189a4b22c655e28c3)), refs [#158](https://github.com/nahuel893/agents-system/issues/158)

## [0.3.1](https://github.com/nahuel893/agents-system/compare/v0.3.0...v0.3.1) (2026-09-27)


### Features

* **evals:** run the whole live suite with scripts/run-live-suite.sh ([#151](https://github.com/nahuel893/agents-system/issues/151)) ([0777ba7](https://github.com/nahuel893/agents-system/commit/0777ba78e345a44e84bc201d646c543bbb265dcd))


### Documentation

* **readme:** document the v0.3.0 platform features ([#152](https://github.com/nahuel893/agents-system/issues/152)) ([5224e5b](https://github.com/nahuel893/agents-system/commit/5224e5bfcf2972c7d4825fd1410a5d4d3db82727))

## [0.3.0](https://github.com/nahuel893/agents-system/compare/v0.2.0...v0.3.0) (2026-09-27)


### ⚠ BREAKING CHANGES

* **roles:** predefined roles no longer declare the session_state tool or the read:session permission. Deployments and importer agents that relied on them must declare and register their own tool.
* **harness:** definitions that relied on the loader failing open now raise DefinitionError: an unrecognised directive, an escalation descriptions value that is not a mapping, a deployment that drops or rewords an inherited escalation condition, changes escalate_to, leaves it out of a replacing escalation_rules mapping or switches audit redaction off, and dataclasses.replace() on an Agent.from_folder agent. sales-agent, data-agent, orchestrator and summary-agent (2.0) restate agent's explicit_user_request, and the last three restate base's confidence_below_threshold too.
* agents_system.demo no longer exists as an importable module; python -m agents_system.demo no longer works. Use uv run python examples/demo/app.py instead.
* **app:** The `{deployment}__{role}` runtime-id scheme and the `_generic` sentinel are removed. Without `create_app(agents=...)`, runtimes come only from `AGENT_REGISTRATIONS` (`{id: "role" | "role@client"}`), and a malformed entry fails boot. `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID` and `DEPLOY_GRANTS` keys are opaque registered ids, and a deployment still configured with old-style ids fails boot until they are registered and renamed. `agents_system.integration.openai_adapter.to_model_id` and `parse_model_id` are deleted; importing either raises `ImportError`.
* **app:** `create_app(agents=...)` is now served. The lifespan builds a runtime for every registered id at boot and fails boot if any entry cannot be resolved, has no grant (in `grants=` or `DEPLOY_GRANTS` under the same id), or has a `grants` value that is not a list. With `agents=`, `WHATSAPP_RUNTIME_ID` and every `ADAPTER_RUNTIMES` id must be registered ids. `clients` is rejected for an `Agent` entry, for an unregistered id, without `agents`, and when a value is not a client-name `str`. Runtime ids longer than 64 characters are rejected.
* **harness:** an extends: value that cannot be placed in the predefined role tree or inside the importer root now raises DefinitionError instead of silently resolving to a same-named predefined role; a valueless extends: key is an error (omit the key when there is no parent).

### Features

* **agent:** Agent Python API (ADR-004 2/10) ([#85](https://github.com/nahuel893/agents-system/issues/85)) ([c4d1ec1](https://github.com/nahuel893/agents-system/commit/c4d1ec18323c36d769305ccca806446d26f55c87))
* **agent:** per-agent provider/model configuration ([#141](https://github.com/nahuel893/agents-system/issues/141)) ([58e30b4](https://github.com/nahuel893/agents-system/commit/58e30b41bf308e04592dd3c1de898a64d5f70074)), refs [#13](https://github.com/nahuel893/agents-system/issues/13)
* **agent:** skills for importer agents (ADR-004 3/10) ([#91](https://github.com/nahuel893/agents-system/issues/91)) ([c50efda](https://github.com/nahuel893/agents-system/commit/c50efda4e41c3bd3b3f10fea6ef0ba252651eb38))
* **app:** create_app accepts agents, grants and clients (ADR-004 4a-i/10) ([#92](https://github.com/nahuel893/agents-system/issues/92)) ([5b5aaf1](https://github.com/nahuel893/agents-system/commit/5b5aaf196cd4ff22c80dacfe03ce0f44e9ee60f6))
* **app:** runtime ids chosen at registration, AGENT_REGISTRATIONS env (ADR-004 4b/10) ([#98](https://github.com/nahuel893/agents-system/issues/98)) ([e5938ca](https://github.com/nahuel893/agents-system/commit/e5938ca603eb3dd79c04e1493c255287a1486dd5))
* **app:** serve registered agents from create_app (ADR-004 4a-ii/10) ([#95](https://github.com/nahuel893/agents-system/issues/95)) ([ca827bd](https://github.com/nahuel893/agents-system/commit/ca827bd803aff72e0e4a9711751d6d4ac0ef535e))
* **deploy:** production units, migrations on deploy and health checks ([#12](https://github.com/nahuel893/agents-system/issues/12)) ([#116](https://github.com/nahuel893/agents-system/issues/116)) ([f22d8ab](https://github.com/nahuel893/agents-system/commit/f22d8abc4885f36734bc2682917a7fe4e4ff6ea1))
* **evals:** gate live tests on success-rate thresholds ([#100](https://github.com/nahuel893/agents-system/issues/100)) ([5a0cd8c](https://github.com/nahuel893/agents-system/commit/5a0cd8c30f013bbee7ae335d8e2d51e5494ce987)), refs [#81](https://github.com/nahuel893/agents-system/issues/81)
* **harness:** agent locator types and disk-reading dispatch (ADR-004 1a/10) ([#68](https://github.com/nahuel893/agents-system/issues/68)) ([2660e00](https://github.com/nahuel893/agents-system/commit/2660e00b1e61a730dcaf2e6c590d190a45d1cd0d))
* **harness:** fail-loud extends resolution with path containment (ADR-004 1b/10) ([#70](https://github.com/nahuel893/agents-system/issues/70)) ([f0ddd41](https://github.com/nahuel893/agents-system/commit/f0ddd414c28f3709e4fe2379e662608ea9666c68))
* **harness:** render escalation condition descriptions into the prompt ([#88](https://github.com/nahuel893/agents-system/issues/88)) ([#89](https://github.com/nahuel893/agents-system/issues/89)) ([9eb44e9](https://github.com/nahuel893/agents-system/commit/9eb44e9a17be10c0dce19d8883ce6f5ef050401e))
* **observability:** /metrics with process, turn and tool metrics ([#78](https://github.com/nahuel893/agents-system/issues/78) 2/3) ([#96](https://github.com/nahuel893/agents-system/issues/96)) ([c40f592](https://github.com/nahuel893/agents-system/commit/c40f5924069142b60808d6560782273d0dc177e2))
* **observability:** real token usage and cost per turn ([#78](https://github.com/nahuel893/agents-system/issues/78) 1/3) ([#87](https://github.com/nahuel893/agents-system/issues/87)) ([5cb206f](https://github.com/nahuel893/agents-system/commit/5cb206f1e59954fbc4f8388521230ec1b31e4fe6))
* **roles:** remove session_state from the predefined roles ([#118](https://github.com/nahuel893/agents-system/issues/118)) ([#122](https://github.com/nahuel893/agents-system/issues/122)) ([f6f0cc2](https://github.com/nahuel893/agents-system/commit/f6f0cc2a4696d60d70efcc5ccba716d7c6c98259))
* **tools:** read-only SQL tool with database-enforced limits ([#80](https://github.com/nahuel893/agents-system/issues/80)) ([#90](https://github.com/nahuel893/agents-system/issues/90)) ([c2c4291](https://github.com/nahuel893/agents-system/commit/c2c4291534f9aadc5269797a851b99ad930e9b91))


### Bug Fixes

* **agent:** validate Agent parameters and ceil every inherited safety field ([#94](https://github.com/nahuel893/agents-system/issues/94)) ([5681a8e](https://github.com/nahuel893/agents-system/commit/5681a8e71dd9f1ad44ef4114f388e121346ac396)), refs [#93](https://github.com/nahuel893/agents-system/issues/93)
* **audit:** drain the sink on shutdown and bound the sequence counter ([#72](https://github.com/nahuel893/agents-system/issues/72)) ([601ce61](https://github.com/nahuel893/agents-system/commit/601ce615785b8f082ac32933a2fc84fc81b2884f)), refs [#8](https://github.com/nahuel893/agents-system/issues/8) [#9](https://github.com/nahuel893/agents-system/issues/9)
* **db:** size every engine's pool from settings ([#11](https://github.com/nahuel893/agents-system/issues/11)) ([#107](https://github.com/nahuel893/agents-system/issues/107)) ([2e925cb](https://github.com/nahuel893/agents-system/commit/2e925cbbde998a2588084a594aca1646eb176afe))
* **deps:** bump starlette past known CVEs ([#69](https://github.com/nahuel893/agents-system/issues/69)) ([f84371a](https://github.com/nahuel893/agents-system/commit/f84371ae7dfd700ee885e6d45234b21f66c8a392)), refs [#33](https://github.com/nahuel893/agents-system/issues/33)
* **deps:** bump torch and setuptools past CVE-2026-59890 ([#73](https://github.com/nahuel893/agents-system/issues/73)) ([d98e699](https://github.com/nahuel893/agents-system/commit/d98e6992c885ff6f7df577020a92c323a9ad9780)), refs [#35](https://github.com/nahuel893/agents-system/issues/35)
* **deps:** bump transformers past CVE-2026-9856 ([#71](https://github.com/nahuel893/agents-system/issues/71)) ([022502a](https://github.com/nahuel893/agents-system/commit/022502a25d2804b699d34c522d2f55ebe36e2043)), refs [#34](https://github.com/nahuel893/agents-system/issues/34)
* **deps:** migrate langchain/langgraph to 1.x and close 15 CVEs ([#32](https://github.com/nahuel893/agents-system/issues/32)) ([#124](https://github.com/nahuel893/agents-system/issues/124)) ([2e53a35](https://github.com/nahuel893/agents-system/commit/2e53a35030d0874f6a8ae15d83368f65b4759879))
* **evals:** close [#113](https://github.com/nahuel893/agents-system/issues/113) review follow-ups (migration env, thread-id, vacuous assertion) ([#123](https://github.com/nahuel893/agents-system/issues/123)) ([ef41a20](https://github.com/nahuel893/agents-system/commit/ef41a20446f2341fd697266f2527fff279987381))
* **evals:** close live-eval guardrail gate and fixture leakage gaps ([#130](https://github.com/nahuel893/agents-system/issues/130)) ([#140](https://github.com/nahuel893/agents-system/issues/140)) ([2397cef](https://github.com/nahuel893/agents-system/commit/2397cef4e1ec2eff22853956b168138a387ff1d4))
* **evals:** stop FakeWhatsAppServer.stop() masking live-harness teardown errors ([#121](https://github.com/nahuel893/agents-system/issues/121)) ([548c4d3](https://github.com/nahuel893/agents-system/commit/548c4d35d80dcbadf284405a93189c972e6a2deb))
* **harness:** close boot-time validation gaps for runtime registration ([#136](https://github.com/nahuel893/agents-system/issues/136)) ([b7e7554](https://github.com/nahuel893/agents-system/commit/b7e755432829511d5ec3289b7bcca13779b03c62)), refs [#128](https://github.com/nahuel893/agents-system/issues/128)
* **harness:** close importer containment defense-in-depth gaps ([#127](https://github.com/nahuel893/agents-system/issues/127)) ([#138](https://github.com/nahuel893/agents-system/issues/138)) ([7eeec7b](https://github.com/nahuel893/agents-system/commit/7eeec7b72d664c9d015686c15e5052c219593f58))
* **harness:** contain every importer folder read and re-validate command tools ([#75](https://github.com/nahuel893/agents-system/issues/75)) ([#97](https://github.com/nahuel893/agents-system/issues/97)) ([df18542](https://github.com/nahuel893/agents-system/commit/df185421e5316203e9efdfe21935f47bd885214b))
* **harness:** fail closed on malformed directives and hold the safety ceiling at deployments ([#106](https://github.com/nahuel893/agents-system/issues/106)) ([#112](https://github.com/nahuel893/agents-system/issues/112)) ([f3e20cc](https://github.com/nahuel893/agents-system/commit/f3e20cc3705ec87ecaa1c0af12ed55a4f515a1b1))
* **harness:** harden escalation description parsing and usage counts ([#88](https://github.com/nahuel893/agents-system/issues/88) follow-up) ([#99](https://github.com/nahuel893/agents-system/issues/99)) ([7f997fd](https://github.com/nahuel893/agents-system/commit/7f997fd9541cff714db1773b1392490d15f51d25))
* **harness:** raise on colliding escalation description keys ([#129](https://github.com/nahuel893/agents-system/issues/129)) ([#137](https://github.com/nahuel893/agents-system/issues/137)) ([614219a](https://github.com/nahuel893/agents-system/commit/614219ace8645b6fdd8e97b8bb6e0760a282c234))
* **health:** report provider reachability and audit drops ([#10](https://github.com/nahuel893/agents-system/issues/10)) ([#109](https://github.com/nahuel893/agents-system/issues/109)) ([4548686](https://github.com/nahuel893/agents-system/commit/45486862bb25dd19a665ba9aa5fa6736b38343fc))
* **observability:** add provider 429/retry metrics and fix catalog_search demo-schema crash ([#145](https://github.com/nahuel893/agents-system/issues/145)) ([#148](https://github.com/nahuel893/agents-system/issues/148)) ([460df3e](https://github.com/nahuel893/agents-system/commit/460df3e29d2c71b686766094bafb7afbdf1c3c75))
* **permissions:** reject unregistered Permission class grants at build time ([#65](https://github.com/nahuel893/agents-system/issues/65)) ([#108](https://github.com/nahuel893/agents-system/issues/108)) ([59fc4ec](https://github.com/nahuel893/agents-system/commit/59fc4ecfda54858e2076b86c02056b2c65559805))
* **platform:** close review follow-ups from the fourth batch ([#146](https://github.com/nahuel893/agents-system/issues/146)) ([#147](https://github.com/nahuel893/agents-system/issues/147)) ([8ee05ed](https://github.com/nahuel893/agents-system/commit/8ee05eda92a9b58f4d489e6c5dd761eb3c51a685))
* **platform:** resolve stale doc, skill, and example references ([3062616](https://github.com/nahuel893/agents-system/commit/306261654a9ca71346d989c3ad28b6555fa64768)), refs [#132](https://github.com/nahuel893/agents-system/issues/132)
* **platform:** resolve stale doc, skill, and example references ([#142](https://github.com/nahuel893/agents-system/issues/142)) ([3062616](https://github.com/nahuel893/agents-system/commit/306261654a9ca71346d989c3ad28b6555fa64768))
* **reference:** give catalog_search a real confidence signal ([#135](https://github.com/nahuel893/agents-system/issues/135)) ([8c7a9dc](https://github.com/nahuel893/agents-system/commit/8c7a9dcb339aa7f9b1f169b16f02d59659f14829)), refs [#117](https://github.com/nahuel893/agents-system/issues/117)
* **release:** keep uv.lock's own package version in sync with releases ([#67](https://github.com/nahuel893/agents-system/issues/67)) ([6660d10](https://github.com/nahuel893/agents-system/commit/6660d1053cc4d453b1d439e617ea2eedfbaa5862))
* **roles:** tie accountant-agent's report-catalog gap to escalation ([#86](https://github.com/nahuel893/agents-system/issues/86)) ([0354d55](https://github.com/nahuel893/agents-system/commit/0354d55592aadca67feb2c81b405790bc261edb7)), refs [#82](https://github.com/nahuel893/agents-system/issues/82)
* **sandbox:** kill the process group when a tool call is cancelled or times out ([#115](https://github.com/nahuel893/agents-system/issues/115)) ([9ab43ac](https://github.com/nahuel893/agents-system/commit/9ab43ac6c196e6e959d40fa31a8692d19b7a1ed4)), refs [#105](https://github.com/nahuel893/agents-system/issues/105)
* **sandbox:** kill the whole sandbox when a call ends while it is still starting ([#105](https://github.com/nahuel893/agents-system/issues/105)) ([#125](https://github.com/nahuel893/agents-system/issues/125)) ([5b36cb8](https://github.com/nahuel893/agents-system/commit/5b36cb8297c99914091672c0c502c55b5778b63a))
* **sql-tool:** close remaining correctness and hardening gaps ([#131](https://github.com/nahuel893/agents-system/issues/131)) ([#143](https://github.com/nahuel893/agents-system/issues/143)) ([4147718](https://github.com/nahuel893/agents-system/commit/414771888f5c1776948247a0c360ddc0d98a7ada))
* **tests:** normalize Unicode dash lookalikes in Guard 1's CHANGELOG role match ([#134](https://github.com/nahuel893/agents-system/issues/134)) ([075c230](https://github.com/nahuel893/agents-system/commit/075c230e044ddb9b6c24666203b1811d091dc122)), refs [#133](https://github.com/nahuel893/agents-system/issues/133)


### Documentation

* **architecture:** add concurrency and scale gaps to ADR-005 ([#63](https://github.com/nahuel893/agents-system/issues/63)) ([1ee65a1](https://github.com/nahuel893/agents-system/commit/1ee65a13c5f9c7b0cb8ce7c0c0a83c220d3a951d))
* **architecture:** ADR-004 and ADR-002 amendments (ADR-004 7/10) ([#111](https://github.com/nahuel893/agents-system/issues/111)) ([04eeb78](https://github.com/nahuel893/agents-system/commit/04eeb7833ad6051bbb37c1d82f13ad53f5d0b11d))
* **delivery:** live-test plan for the full agent lifecycle ([#77](https://github.com/nahuel893/agents-system/issues/77)) ([9cf601d](https://github.com/nahuel893/agents-system/commit/9cf601dadf31b32b6708e4e245d095ebb2d0cad9)), refs [#76](https://github.com/nahuel893/agents-system/issues/76)
* **openspec:** fix stale cross-references left by the library-first-agents archive move ([#120](https://github.com/nahuel893/agents-system/issues/120)) ([6918283](https://github.com/nahuel893/agents-system/commit/691828383b6fe5952f3d45032252ba3135b85a47))
* **openspec:** library-first-agents proposal, specs, design and tasks (ADR-004) ([#66](https://github.com/nahuel893/agents-system/issues/66)) ([637159c](https://github.com/nahuel893/agents-system/commit/637159c03ed91655b0560cc81465e0b8949f8187))
* **openspec:** verify and archive library-first-agents (ADR-004) ([#114](https://github.com/nahuel893/agents-system/issues/114)) ([6e6b50d](https://github.com/nahuel893/agents-system/commit/6e6b50d7e1e8532cc15e93c3a6b725b73541a85e))
* **openspec:** verify and archive permission-model; add library-first-agents exploration ([#64](https://github.com/nahuel893/agents-system/issues/64)) ([e3b2222](https://github.com/nahuel893/agents-system/commit/e3b22223208fabebb7ec3a0f9b33db23d587a856)), refs [#38](https://github.com/nahuel893/agents-system/issues/38)
* **process:** add the PR template with the four delivery questions ([#16](https://github.com/nahuel893/agents-system/issues/16)) ([#110](https://github.com/nahuel893/agents-system/issues/110)) ([f8430c1](https://github.com/nahuel893/agents-system/commit/f8430c1bb723029ae787170f29e3e751bc592ea2))


### Build System

* **deps-dev:** bump mypy from 1.20.0 to 2.3.1 ([#2](https://github.com/nahuel893/agents-system/issues/2)) ([a9f52a6](https://github.com/nahuel893/agents-system/commit/a9f52a68270cb5c08dadadfd43d663327a56cc98))
* **deps:** bump redis from 5.3.1 to 8.1.0 ([#5](https://github.com/nahuel893/agents-system/issues/5)) ([93ad89f](https://github.com/nahuel893/agents-system/commit/93ad89f48ecfbe81a3bec171b8d91a4cce647e8e))
* **deps:** bump sentence-transformers from 5.4.1 to 6.1.0 ([#4](https://github.com/nahuel893/agents-system/issues/4)) ([89ce7fa](https://github.com/nahuel893/agents-system/commit/89ce7fa4b6da9e1380315ab1e3bc6db4d97288e5))
* **deps:** bump structlog from 25.5.0 to 26.1.0 ([#3](https://github.com/nahuel893/agents-system/issues/3)) ([d4c8986](https://github.com/nahuel893/agents-system/commit/d4c8986206a5c723b3339ef4315d205e63737b4d))
* **deps:** bump the uv-minor-patch group across 1 directory with 5 updates ([#74](https://github.com/nahuel893/agents-system/issues/74)) ([c62dbc4](https://github.com/nahuel893/agents-system/commit/c62dbc4acf5f5497075911a3919f983d1abe0957))


### Code Refactoring

* rename generic roles to predefined and move the demo to examples/ (ADR-004 5/10) ([#101](https://github.com/nahuel893/agents-system/issues/101)) ([7320fb3](https://github.com/nahuel893/agents-system/commit/7320fb3263d16d3d44fe95643ce1414106a1c0f0))


### Continuous Integration

* **roles:** guard predefined role changes with a reviewed snapshot and version bump (ADR-004 6/10) ([#103](https://github.com/nahuel893/agents-system/issues/103)) ([2da6a45](https://github.com/nahuel893/agents-system/commit/2da6a45b3d07f6129ca19c199b982f0e506dcfd0))

## [0.2.0](https://github.com/nahuel893/agents-system/compare/v0.1.0...v0.2.0) (2026-09-25)


### ⚠ BREAKING CHANGES

* **permissions:** booting the app now requires DEPLOY_GRANTS for every configured runtime; see docs/architecture/permission-model.md.
* rename package to agents-system ([#189](https://github.com/nahuel893/agents-system/issues/189))

### Features

* **demo:** documented entrypoint to serve the OpenAI-compatible API over the demo database ([#53](https://github.com/nahuel893/agents-system/issues/53)) ([4843959](https://github.com/nahuel893/agents-system/commit/4843959e15c3831c9ac4fc4eccfa96e8fdb32d2a)), refs [#46](https://github.com/nahuel893/agents-system/issues/46)
* **permissions:** add Permission class hierarchy and registry (permission-model 1/4) ([#43](https://github.com/nahuel893/agents-system/issues/43)) ([7a209e8](https://github.com/nahuel893/agents-system/commit/7a209e879304d5b960844dada06f0463e93e1e67))
* **permissions:** enforce tier rules R2-R4 in ToolSpec, injector and loader (permission-model 2/4) ([#50](https://github.com/nahuel893/agents-system/issues/50)) ([817410c](https://github.com/nahuel893/agents-system/commit/817410cb3b2834b9b960c001fb0ac134f6832435))


### Bug Fixes

* **adapter:** bound POST /v1/chat/completions body size ([#37](https://github.com/nahuel893/agents-system/issues/37)) ([#40](https://github.com/nahuel893/agents-system/issues/40)) ([fe7f53c](https://github.com/nahuel893/agents-system/commit/fe7f53c1ab3416babca8a64414ddf51d63b6fc46))
* **evals:** explicit grants instead of implicit full-role grant ([#58](https://github.com/nahuel893/agents-system/issues/58)) ([91f2daf](https://github.com/nahuel893/agents-system/commit/91f2daf9c6a24bc774212e4f4518664ab707cc28)), refs [#47](https://github.com/nahuel893/agents-system/issues/47)
* **harness:** render escalation_rules into the composed system prompt ([#42](https://github.com/nahuel893/agents-system/issues/42)) ([f02ea0a](https://github.com/nahuel893/agents-system/commit/f02ea0a892bd9679e94dd79cbf3e169289c178b3))
* **permissions:** explicit deploy grants and Layer-2 grant ceiling (permission-model 3/4) ([#55](https://github.com/nahuel893/agents-system/issues/55)) ([a13dacc](https://github.com/nahuel893/agents-system/commit/a13daccdc70889f6ed252a6292aa85306db8bbd4))
* **reports:** expose per-report parameters in the run_report tool schema ([#54](https://github.com/nahuel893/agents-system/issues/54)) ([4b062db](https://github.com/nahuel893/agents-system/commit/4b062db6988a78b5b939d791c962a85174233979)), refs [#52](https://github.com/nahuel893/agents-system/issues/52)
* **reports:** rank top products by units before truncating ([#51](https://github.com/nahuel893/agents-system/issues/51)) ([cdf5d07](https://github.com/nahuel893/agents-system/commit/cdf5d074d6e594bf48f3d21873c5f319fd7810ff)), refs [#44](https://github.com/nahuel893/agents-system/issues/44)
* resolve ruff 0.16 lint findings (2437fcb) switched the drain-failure ([464753c](https://github.com/nahuel893/agents-system/commit/464753ced26d2d0aa8145fa529d01ed34c1e0545))


### Performance Improvements

* **reports:** describe shared run_report parameters once ([#59](https://github.com/nahuel893/agents-system/issues/59)) ([ffba3e0](https://github.com/nahuel893/agents-system/commit/ffba3e0370f5605f835c4b4a2130c6d868da514e)), refs [#56](https://github.com/nahuel893/agents-system/issues/56)


### Documentation

* **architecture:** ADR-005 operational safety and governance roadmap (proposed) ([#60](https://github.com/nahuel893/agents-system/issues/60)) ([f0e4b2b](https://github.com/nahuel893/agents-system/commit/f0e4b2bc13dfb30bbf9d91a4cfa978363c6c5a20))
* **permissions:** ADR-003 permission hierarchy (permission-model 4/4) ([#57](https://github.com/nahuel893/agents-system/issues/57)) ([68852d4](https://github.com/nahuel893/agents-system/commit/68852d450206518574097320035c3af141c77747))
* **reports:** migration for the agents_system_* reporting view rename ([#49](https://github.com/nahuel893/agents-system/issues/49)) ([28360ed](https://github.com/nahuel893/agents-system/commit/28360ed31f649b6574f2a0c21b5ef5f4e3b728de)), refs [#45](https://github.com/nahuel893/agents-system/issues/45)


### Build System

* adopt ruff 0.16 ([#39](https://github.com/nahuel893/agents-system/issues/39)) ([464753c](https://github.com/nahuel893/agents-system/commit/464753ced26d2d0aa8145fa529d01ed34c1e0545))
* rename package to agents-system ([#189](https://github.com/nahuel893/agents-system/issues/189)) ([6402f58](https://github.com/nahuel893/agents-system/commit/6402f581b695900b7abf9fe2dc4167f29ff300b8))


### Continuous Integration

* automate SemVer releases and CHANGELOG with release-please ([#48](https://github.com/nahuel893/agents-system/issues/48)) ([6778fea](https://github.com/nahuel893/agents-system/commit/6778fea7b608e2ab790b81ec450293edb8a1224e))
* cut releases locally with release-please and plain vX.Y.Z tags ([#62](https://github.com/nahuel893/agents-system/issues/62)) ([487ec68](https://github.com/nahuel893/agents-system/commit/487ec687f28db7b8dc59a41bc5a1aaeaa80d1393)), refs [#18](https://github.com/nahuel893/agents-system/issues/18)

## Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are generated from [Conventional Commits](https://www.conventionalcommits.org/)
by [release-please](https://github.com/googleapis/release-please) — see
`docs/operations/release-process.md` for how a release PR is cut and merged.
