Required Notice: Copyright (c) 2026 KROZ-coding (https://github.com/KROZ-coding/audit-companion)

This repository contains code derived from audit-companion, licensed under PolyForm Noncommercial License 1.0.0. The complete license text is in the root LICENSE file and is also available at https://polyformproject.org/licenses/noncommercial/1.0.0.

Upstream source revision: d01ab02d287641e5c62b731a7941b9a5c4e07b29.
Adaptations include a new FastAPI entry point, StudyStore subclass, fictional mathematics sample API, backend-computed diagnosis and scoped exports, practice persistence, and the frontend adapter. The upstream Store has a connection-cleanup fix; settings, chat prompts, and MaxKB URL handling are adapted. llm_client.py is a compact compatibility implementation, not the original concurrency/rate-limit/streaming pool. Only the study routes are registered in the local sample app; retained audit-course modules remain unadapted unless explicitly documented otherwise. See docs/本地部署与承接说明.md for the precise imported and excluded scope.

The license permits noncommercial purposes only. Commercial use requires separate authorization from the licensor.
