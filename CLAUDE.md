# Project Instructions for Claude

## Version History Management

When making significant changes, bug fixes, or new features:

1. Update the version history in `README.md`
2. Add a new version entry at the **top** (newest first)
3. Follow the existing format:
   ```
   ### X.X.X - Short Title
   - Brief description of change
   - Another change if applicable
   ```
4. Version numbering:
   - **Patch (X.X.+1)**: Bug fixes, minor tweaks
   - **Minor (X.+1.0)**: New features, enhancements
   - **Major (+1.0.0)**: Breaking changes, major overhauls

## Commit Guidelines

- Use conventional commit messages when possible (feat:, fix:, docs:, etc.)
- Reference the version number in commits when updating README version history

## Project-Specific Notes

### App Version Location
- **Environment Variable**: `APP_VERSION` (set in Azure Static Web App configuration)
- **API Endpoint**: `/api/get-config` returns `appVersion` (see `api/GetConfig/__init__.py`)
- **Frontend Fallback**: `index.html:1012` (`let APP_VERSION`) if the API is unavailable
- **Display**: Version shown in header via `#versionDisplay` element

### When Releasing a New Version
1. Update `APP_VERSION` environment variable in Azure
2. Update the fallback version in `api/GetConfig/__init__.py` (line 24)
3. Update the fallback version in `index.html` (line 1012)
4. Add version entry to `README.md` version history

## Environment

Mike works from a cloud desktop (Windows with WSL) as of November 2026, so one machine serves home and office. Repos live at `/home/mjones/Projects/<repo>`, as before, and Claude's per-repo memory was copied across from the old laptop in November 2026. Two quirks recorded on the old laptop are still true here, measured 2026-11-05: `az` is the Windows CLI through WSL interop (`/mnt/c/Program Files (x86)/.../az`; its Windows-path warning goes to stderr, and `-o tsv` can carry a trailing `\r`), and `/tmp` is wiped on every reboot (nothing there predates the current boot). Other old-laptop notes in memory: check before applying them here.
