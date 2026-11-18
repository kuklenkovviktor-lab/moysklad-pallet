# GRIT WMS engineering and design conventions

## Non-negotiable UI standard

**Every new GRIT WMS screen, module, dialog, navigation transition and feature must use the existing GRIT dark design on its first implementation.** Never introduce white/light default forms, cards, tables, menus, modals, empty states or loading screens and plan to fix their appearance later.

Use the shared palette and CSS in `grit_theme.py` for manager screens, or the same palette in isolated terminal/partner screens:
- page background: `#09121e`
- card/panel: `#142337`, with subtle `#16283b` / `#101c2a` gradients when needed
- panel border: `#30465c`
- primary text: `#f1f6ff`
- secondary text: `#9eb5ca`
- interactive blue: `#176eae` (hover `#2488cf`)
- emphasis/focus orange: `#ff9230`
- input background: `#0d1d2c`, input border `#42607a`
- blue-gray secondary buttons: `#233b50`

Preserve familiar layout/navigation, compact touch targets, 9–13px rounded corners, consistent spacing, accessible contrast and visible focus. Include matching `<meta name="theme-color" content="#09121e">` on standalone screens, including login and startup states. Do not let browser/mobile transitions flash white.

**White is permitted only where functionally necessary**: printed documents/labels, QR and barcodes with a white high-contrast backing, and camera/scanner rendering surfaces that require it. White QR backing is not a light application card. Explicitly preserve the `@media print` behavior for A4/58×40 mm labels and staff badges.

Prefer extending existing design primitives over creating competing inline palettes. When editing an old screen, eliminate mismatched light surfaces in the same change. Include regression coverage for the dark background and printing/QR exceptions, and ensure no change to warehouse/business operations.

## Safety and delivery

Use synthetic isolated test data. Do not execute trial operations against the employer's warehouse or real marketplace supplies. GitHub stores code, branches and pull requests only. Never use GitHub Actions for tests, builds or production deployment. Run isolated synthetic tests and controlled app-only releases on HOSTKEY directly, after verification; preserve production PostgreSQL and all Compose overlays. Respect backups and existing production data. Do not overwrite DB, secret keys or employee history to implement a visual change.
