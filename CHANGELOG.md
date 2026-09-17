# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Initial repo scaffolding: README, MIT license, `.gitignore`, `.gitattributes`
  (enforcing LF line endings), `.editorconfig`, `requirements.txt` /
  `requirements-dev.txt`, `.env.example`, `docs/ARCHITECTURE.md`, `CLAUDE.md`.
- No application code yet — see `docs/ARCHITECTURE.md` for the planned design.

### Changed

- Reddit access plan reversed from PRAW/OAuth to unauthenticated fetching as the primary
  path, after confirming Reddit's November 2025 "Responsible Builder Policy" blocks
  essentially all new developer app registrations in practice (hit this directly trying
  to register this project's own app). See `docs/ARCHITECTURE.md` → "Reddit access".
