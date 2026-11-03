"""Shared visual system for manager-facing GRIT WMS pages.

CSS only. Routes, API calls and business operations are intentionally untouched.
A strict route allowlist in app.py determines where the theme is injected.
"""
GRIT_CSS = r"""
:root{color-scheme:dark;--g-bg:#09121e;--g-nav:#0b1827;--g-surface:#142337;--g-surface2:#192e43;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#2d92e2;--g-orange:#ff9230;--g-green:#47cc90}
*{scrollbar-color:#35536c #0e1c2b}
body{background:var(--g-bg)!important;color:var(--g-text)!important;font-family:system-ui,-apple-system,"Segoe UI",Arial,sans-serif!important}
body .sidebar,body aside.sidebar{background:linear-gradient(180deg,#0b1726,#0c1a29)!important;border-color:#294052!important}
body .brand{border-color:#293e52!important;color:#f3f8ff!important}
body .brand span,body .small,body .muted,body .details,body .time,body .empty,body .meta,body .caption{color:var(--g-muted)!important}
body .side-nav a{background:transparent!important;color:#dce8f3!important;border-color:transparent!important;font-weight:650!important}
body .side-nav a:hover{background:#20374c!important;color:white!important}
body .side-nav a.active{background:linear-gradient(105deg,#1471c4,#17549b)!important;border-color:#2c85c6!important;color:white!important}
body .side-nav a.logout{background:#36242c!important;color:#ffc1b8!important}
body .card,body .panel,body .toolbar,body .head:not(.top),body .details:not(.small),body .register,body .status,body .notice,body .metric,body .info-panel{
background:linear-gradient(145deg,#16283b,#101c2a)!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important;border-radius:12px!important}
body .kpi,body .mini,body .metric>div,body .metric .value,body .badge:not(.print-target),body .linebtn,body .row-card{
background:#1b3045!important;border-color:#345069!important;color:var(--g-text)!important}
body .kpi .v,body .mini .n,body .metric b{color:#fff!important}
/* Shift history: these cards are .stat, not .kpi or .metric. */
body .summary .stat{background:#1b3045!important;border:1px solid #345069!important;color:#f1f6ff!important}
body .summary .stat .small{color:#9eb5ca!important}
body .summary .stat .value{color:#ffffff!important;font-variant-numeric:tabular-nums}
body h1,body h2,body h3,body h4,body strong,body .section-title b{color:#f3f8fd!important}
body a{color:#75bef9}
body a:hover{color:#b9e0ff}
body button,body .action,body a.button,body .print-one,body input[type=submit]{background:#176eae!important;color:white!important;border:1px solid #358acc!important;border-radius:9px!important}
body button:hover,body .action:hover,body a.button:hover{background:#2086d0!important}
body button.secondary,body .secondary,body .top a,body .linebtn{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body input:not([type=checkbox]):not([type=radio]),body select,body textarea{
background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px!important}
body input::placeholder,body textarea::placeholder{color:#819bb1!important}
body input:focus-visible,body select:focus-visible,body textarea:focus-visible,body button:focus-visible,body a:focus-visible{
outline:2px solid var(--g-orange)!important;outline-offset:2px}
body label,body .period-filters label{color:#b9cde0!important}
body table{background:#111f2f!important;color:#eef5fd!important}
body th{background:#1b3146!important;color:#b8d7ed!important;border-color:#345068!important}
body td{background:transparent!important;color:#e4eef8!important;border-color:#2b4255!important}
body tr:hover td{background:#1e3549!important}
body .table-wrap{border-color:#355269!important}
body .worker,body .dev,body .shift,body .history,body .event,body .row{border-color:#2c4357!important}
body .note{background:#35291d!important;border-color:#805b34!important;color:#ffe0ad!important}
body .warn,body .data-warning,body .type-debt{color:#ffba64!important}
body .danger,body .error{color:#ff8f91!important}
body .ok,body .type-normal{color:#63d8a0!important}
body .top a{display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
body .progress{background:#283e54!important}
body .progress>div{background:linear-gradient(90deg,#288ce0,#ff942f)!important}
body .status:not(.badge),body .notice{border-left:3px solid #2d92e2!important}
body .grid,body .kpis,body .filters{gap:12px}
body .section-link{color:#75bdf8!important}
/* Interactive SVG warehouse map preserves its coordinate and state colors. */
body svg{max-width:100%}
@media(max-width:760px){body .content{padding:12px!important}body .wrap{padding-left:12px!important;padding-right:12px!important}}
/* Printed staff credentials and pallet labels must remain high-contrast on white. */
@media print{
 :root{color-scheme:light}
 body{background:#fff!important;color:#000!important}
 body .badge,body .badge:not(.print-target),body .print-target,body .qr{background:#fff!important;color:#000!important;border-color:#ddd!important}
 body .badge *,body .print-target *{color:#000!important}
 body .top,body .print-one{display:none!important}
}
"""
