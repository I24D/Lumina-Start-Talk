# Contributing to Lumina Start Talk

Thanks for being here. Lumina Start Talk is part of the **Lumina IA** family, maintained by
**Dal Nijaruq** ([@I24D](https://github.com/I24D)). Contributions are welcome from anyone.

**Write in English or Spanish — both are fine.** Issues, pull requests and discussions in
Spanish are read and answered. *Puedes escribir en español sin problema.*

---

## Ways to help

| | |
|---|---|
| 🐛 **Report a bug** | [Open an issue](../../issues/new/choose). Include your OS, Python version, and the Activity Log lines around the failure |
| 🧩 **Write a plugin** | The easiest and most valuable contribution — see below |
| 🌍 **Translate** | The assistant adapts to the user's language, but docs and UI strings can always improve |
| 📖 **Improve the docs** | If something took you a while to figure out, that is a documentation bug |
| 💡 **Propose a feature** | Start a [discussion](../../discussions) before writing a lot of code |

---

## Writing a plugin — the best place to start

Lumina learns new skills from single files. You never have to touch the core.

1. Copy `plugins/_template.py` to `plugins/your_skill.py` (no leading underscore).
2. Fill in the `PLUGIN` dict — `name`, `description`, `parameters`.
3. Write `run(parameters, player=None, session_memory=None)` and return a short sentence.
4. Restart Lumina. It is discovered automatically.

Two things matter more than anything else:

- **The `description` is the whole interface.** It is what the model reads to decide whether to
  call your tool. Be explicit about the phrases that should trigger it, in every language you
  expect, and say which other tool to prefer when yours could be confused with it. A vague
  description means your plugin silently never runs.
- **Never raise.** Catch your own errors and return a spoken error string. The loader catches
  exceptions as a safety net, but a crash inside your plugin should not reach the user as one.

`plugins/phone_notifications.py` is a worked example of a non-trivial plugin: multiple actions,
a fallback data source, filtering, and a background thread.

---

## Pull requests

- Branch from `main`, one focused change per PR.
- Match the surrounding code: same naming, same comment density, no reformatting of untouched
  lines. Comments explain *why*, not *what*.
- Say in the PR **how you tested it**. "Ran the app and asked X, got Y" is a real test and is
  worth more than a description of what the code should do.
- If your change touches Windows-, macOS- or Linux-specific behaviour, say which of them you
  actually ran it on. Nobody has all three; being honest about coverage helps the reviewer.

---

## Reporting bugs well

The single most useful thing you can include is **what the Activity Log said**, plus the console
output if you launched with `python main.py` rather than `pythonw.exe`.

Please make sure that:

- Your report has **no API keys, tokens or personal messages** in it. Notification and message
  content is easy to paste by accident — redact it.
- You mention whether Lumina is running as administrator, since a few features (hardware
  temperature, some system controls) behave differently without it.

---

## Code of conduct

Be decent. Assume good faith, keep criticism about the code, and remember that many people here
are not writing in their first language. Harassment of any kind is not welcome, and the
maintainer may remove comments or contributors that make the project worse to be part of.

---

## Licence

This project is licensed under **[CC BY-NC 4.0](LICENSE)**, inherited from
[MARK LII](https://github.com/FatihMakes/Mark-LII) by
[FatihMakes](https://github.com/FatihMakes). By contributing you agree that your work is
released under the same terms, which means **non-commercial use only** and that attribution to
the original author stays in place.
