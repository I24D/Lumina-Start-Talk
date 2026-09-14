# Learning English third-party notes

Lumina's Learning English implementation is original code. The following
projects are dependencies, optional integrations, or product references.

## Runtime dependency

- Py-FSRS (`fsrs`) — MIT License. Used to schedule vocabulary reviews.
  <https://github.com/open-spaced-repetition/py-fsrs>

## Optional local integrations

- OpenPronounce — MIT License. Lumina contains an adapter but does not bundle
  the package, speech models, ffmpeg, espeak-ng or model weights.
  <https://github.com/Halleck45/OpenPronounce>
- LanguageTool — LGPL-2.1-or-later. Lumina can call a separately operated local
  LanguageTool HTTP server; LanguageTool itself is not bundled.
  <https://github.com/languagetool-org/languagetool>

## Product references only

- AI Tutor — MIT License. Referenced for scenario and learning-dashboard ideas.
  <https://github.com/ly-rrrrr/ai-tutor>
- FreeLingo — AGPL-3.0/commercial. Referenced for educational concepts only
  (placement, CEFR route, unit checkpoints, weekly goals); no source files,
  prompts or course data were copied.
  <https://github.com/ArtCC/freelingo>
- OmniLingo — AGPL-3.0. Referenced for listening-exercise concepts only; no
  source files, audio or datasets were copied.
  <https://github.com/omnilingo/omnilingo>
- LinguaCafe — GPL-3.0. Referenced for the assisted-reading idea behind guided
  readings (a glossary the reader can save for review); no source files were
  copied. <https://github.com/simjanos-dev/LinguaCafe>

The CEFR route (Pre-A1 to C2), the scenarios, the placement test question bank
and the generation prompts are written for Lumina.
