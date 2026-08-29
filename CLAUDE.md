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
- **Frontend Fallback**: `index.html` — the `let APP_VERSION = '...'` declaration, used if the API is unavailable
- **Display**: Version shown in header via `#versionDisplay` element

### When Releasing a New Version
1. Update `APP_VERSION` environment variable in Azure
2. Update the fallback in `api/GetConfig/__init__.py` — the `os.environ.get('APP_VERSION', '...')` default
3. Update the fallback in `index.html` — the `let APP_VERSION = '...'` declaration

   (Both are referenced by symbol rather than line number, which drifts.)
4. Add version entry to `README.md` version history
