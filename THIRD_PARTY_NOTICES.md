# Learning English third-party notes

Lumina's Learning English implementation is original code. The following
projects are dependencies, local engines, a hosted model, or product references.

## Runtime dependency

- Py-FSRS (`fsrs`) — MIT License. Used to schedule vocabulary reviews.
  <https://github.com/open-spaced-repetition/py-fsrs>

## Local engines, installed on demand

`python -m learning_english.engines install` downloads these into
`%LOCALAPPDATA%\LuminaStartTalk\engines`. None of them is part of this
repository, and each runs unmodified in its own process.

- LanguageTool — LGPL-2.1-or-later. Run as a loopback-only HTTP server.
  <https://github.com/languagetool-org/languagetool>
- Eclipse Temurin JRE 21 — GPL-2.0 with the Classpath Exception. The Java runtime
  LanguageTool runs on. <https://adoptium.net/>
- OpenPronounce — MIT License. Run in its own Python environment.
  <https://github.com/Halleck45/OpenPronounce>
- eSpeak NG — GPL-3.0. Unpacked for OpenPronounce's phonemizer.
  <https://github.com/espeak-ng/espeak-ng>
- `facebook/wav2vec2-large-960h` and `facebook/wav2vec2-lv-60-espeak-cv-ft` —
  Apache-2.0. Speech models OpenPronounce downloads from Hugging Face.

## Hosted model

- Gemma 4 (`gemma4:31b`), served by Ollama Cloud. Lumina sends it the text of a
  student's turn for analysis; its use follows the model's own license and
  Ollama's terms. <https://ollama.com/library/gemma4>

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
