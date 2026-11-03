# Changelog

## 1.1.0 (2026-11-03)

This fork's first release on top of The Hunter skill 1.0 by Jake Schincariol.

### UK support

- `region: "UK"` in the profile, with `profile.uk.example.json` to start from.
- Scout: UK locations and cities (with US namesakes such as Cambridge, MA ruled
  out), right to work in the UK, remote EMEA and Europe roles counted as open,
  five UK boards (Monzo, Google DeepMind, GoCardless, Cleo, Octopus Energy),
  manual routes for Wise and Civil Service Jobs, and `--region UK`.
- Research: Companies House records in the brief (`--uk`, `--ch-number`, and a
  new `ch` command), with a free API key.
- Resume: `ats.py cv` checks UK CV conventions. British spellings of strong
  verbs (optimised, analysed, modelled) now score the same as US ones.
- Apply: notice period, degree classification, A-levels, Skilled Worker visa,
  UK equality monitoring, DBS and National Insurance questions. The last ones
  are never filled for the user.
- Salary: `offer.py uk` for take-home pay from HMRC's 2026 to 2027 rates
  (`uk-tax.json`), including Scottish bands, student loans and pensions. Pounds
  and euros print with their symbols.

### New

- `pipeline.py export --format ics` (calendar) and `--format html` (dashboard).
- `answers.py bank`: approved answers shared by cover letters, forms and
  interviews, and `bank check` for unsupported figures in a draft.
- GitHub Actions: tests on every push (Python 3.8 and 3.12) and a weekly live
  check of every job board.

### Fixes

- The scout no longer reads "OR", "IN", "ME" and the like in a location as US
  state codes, or the word "us" as the United States.
- `offer.py bls` defaults to the latest OEWS year BLS has published, worked out
  from today's date, instead of a fixed 2025.
- `.gitignore` for caches and a local `hunter/` folder; the repo layout test no
  longer breaks on dotfiles.
- Manifests, README and the tools' User-Agent point to Ltyrrell1/hunter-skill.
