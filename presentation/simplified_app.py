"""Small HTML views for the household-only PowerMonitor experience."""

from __future__ import annotations

import html
from datetime import date
from typing import Any


def money(value: Any) -> str:
    return "Unavailable" if value is None else f"${float(value):,.2f}"


def energy(value: Any) -> str:
    return f"{float(value or 0):,.2f} kWh"


def layout(title: str, active: str, body: str) -> str:
    nav = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
        for key, label, url in (
            ("dashboard", "Dashboard", "/"),
            ("ev", "EV Sessions", "/ev"),
            ("settings", "Settings", "/settings"),
        )
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · PowerMonitor</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f5f7f6;color:#18251f;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:#123c2d;color:white;padding:18px max(20px,calc((100vw - 1080px)/2));display:flex;align-items:center;gap:32px}} header strong{{font-size:19px}} nav{{display:flex;gap:7px}} nav a{{color:#dce9e2;text-decoration:none;padding:7px 10px;border-radius:7px}} nav a.active{{background:#fff;color:#123c2d}}
main{{max-width:1080px;margin:0 auto;padding:30px 20px 56px}} h1{{margin:0 0 6px;font-size:28px}} h2{{font-size:18px;margin:0 0 12px}} p.subtle,.muted{{color:#64736b}} .period{{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:24px 0}} .period form{{display:flex;flex-wrap:wrap;gap:8px;align-items:end}} button,.button{{border:1px solid #1d6a4a;border-radius:7px;background:#1d6a4a;color:#fff;padding:9px 12px;font:inherit;text-decoration:none;cursor:pointer}} .button.secondary,button.secondary{{background:#fff;color:#1d6a4a}} label{{display:grid;gap:4px;font-size:13px;color:#47574f}} input,select{{padding:8px;border:1px solid #b8c5bd;border-radius:6px;background:#fff;font:inherit}} .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}} .metric,.panel{{background:#fff;border:1px solid #dbe3de;border-radius:10px;padding:16px}} .metric span{{display:block;color:#65756c;font-size:13px;margin-bottom:6px}} .metric strong{{font-size:22px}} .warning{{background:#fff4d9;border:1px solid #f0d28d;padding:12px 14px;border-radius:8px;margin:14px 0}} .grid{{display:grid;grid-template-columns:2fr 1fr;gap:16px}} table{{border-collapse:collapse;width:100%}} th,td{{padding:10px 6px;border-bottom:1px solid #e5ebe7;text-align:left}} th{{font-size:12px;color:#64736b}} .chart{{width:100%;height:220px;display:block}} .bar{{fill:#4f9273}} .evbar{{fill:#d78a41}} .legend{{display:flex;gap:15px;color:#64736b;font-size:13px}} .dot{{width:10px;height:10px;display:inline-block;border-radius:2px;margin-right:4px}} .empty{{padding:20px;color:#64736b}} .settings-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} code{{font-size:12px}} @media(max-width:720px){{header{{display:block}}nav{{margin-top:12px}}.metrics,.grid,.settings-grid{{grid-template-columns:1fr}}}}
</style></head><body><header><strong>PowerMonitor</strong><nav>{nav}</nav></header><main>{body}</main></body></html>"""


def period_controls(period: str, start: str, end: str) -> str:
    options = "".join(
        f'<a class="button {"secondary" if key != period else ""}" href="/?period={key}">{label}</a>'
        for key, label in (("this_month", "This month"), ("last_month", "Last month"), ("this_year", "This year"))
    )
    return f"""<section class="period"><form method="get">{options}
      <input type="hidden" name="period" value="custom"><label>From<input type="date" name="start" value="{html.escape(start)}" required></label>
      <label>To<input type="date" name="end" value="{html.escape(end)}" required></label><button type="submit">Apply</button></form></section>"""


def usage_chart(daily: list[dict[str, Any]], ev_sessions: list[dict[str, Any]]) -> str:
    if not daily:
        return '<div class="empty">No household readings are available for this period.</div>'
    ev_by_day: dict[str, float] = {}
    for session in ev_sessions:
        day = str(session["start_time"])[:10]
        ev_by_day[day] = ev_by_day.get(day, 0.0) + float(session["energy_kwh"])
    shown = daily if len(daily) <= 62 else _weekly(daily, ev_by_day)
    maximum = max(max(float(row["grid_import_kwh"]), ev_by_day.get(row["date"], 0.0)) for row in shown) or 1.0
    width, height, base = 900, 185, 165
    slot = width / max(len(shown), 1)
    bars = []
    for index, row in enumerate(shown):
        x = index * slot + slot * .15
        household_height = float(row["grid_import_kwh"]) / maximum * 140
        ev_height = ev_by_day.get(row["date"], 0.0) / maximum * 140
        bars.append(f'<rect class="bar" x="{x:.1f}" y="{base-household_height:.1f}" width="{slot*.7:.1f}" height="{household_height:.1f}"><title>{html.escape(row["date"])}: {energy(row["grid_import_kwh"])}</title></rect>')
        if ev_height:
            bars.append(f'<rect class="evbar" x="{x+slot*.22:.1f}" y="{base-ev_height:.1f}" width="{slot*.25:.1f}" height="{ev_height:.1f}"><title>EV: {energy(ev_by_day[row["date"]])}</title></rect>')
    bars_html = "".join(bars)
    return f'<div class="legend"><span><i class="dot" style="background:#4f9273"></i>Household grid import</span><span><i class="dot" style="background:#d78a41"></i>EV charging</span></div><svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Daily household usage and EV charging">{bars_html}<line x1="0" y1="{base}" x2="{width}" y2="{base}" stroke="#bdc9c1"/></svg>'


def _weekly(daily: list[dict[str, Any]], ev_by_day: dict[str, float]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in daily:
        week = date.fromisoformat(row["date"]).strftime("%G-W%V")
        item = grouped.setdefault(week, {"date": week, "grid_import_kwh": 0.0, "ev_kwh": 0.0})
        item["grid_import_kwh"] += float(row["grid_import_kwh"])
        item["ev_kwh"] += ev_by_day.get(row["date"], 0.0)
    ev_by_day.clear()
    ev_by_day.update({item["date"]: item["ev_kwh"] for item in grouped.values()})
    return list(grouped.values())


def dashboard(summary: dict[str, Any], period: str, start: str, end: str) -> str:
    household, ev, tariff = summary["household"], summary["ev"], summary["tariff"]
    warnings = "".join(f'<div class="warning">{html.escape(warning)}</div>' for warning in tariff["warnings"])
    percentage = f'<p class="muted">EV: {ev["percentage_of_household"]:.1f}% of household grid consumption</p>' if ev["percentage_of_household"] is not None else ""
    sessions = "".join(f'<li>{html.escape(str(item["start_time"])[:16])} · {energy(item["energy_kwh"])}</li>' for item in ev["sessions"][:5]) or "<li>No EV sessions in this period.</li>"
    body = f"""<h1>Household electricity</h1><p class="subtle">{html.escape(start)} to {html.escape(end)}</p>{period_controls(period, start, end)}{warnings}
<section class="metrics"><div class="metric"><span>Household usage</span><strong>{energy(household["grid_import_kwh"])}</strong></div><div class="metric"><span>Household cost</span><strong>{money(household["cost"])}</strong></div><div class="metric"><span>EV charging</span><strong>{energy(ev["energy_kwh"])}</strong></div><div class="metric"><span>EV charging cost</span><strong>{money(ev["cost"])}</strong></div></section>{percentage}
<section class="grid"><div class="panel"><h2>Daily household usage</h2>{usage_chart(household["daily"], ev["sessions"])}</div><div class="panel"><h2>EV activity</h2><ul>{sessions}</ul><a class="button secondary" href="/ev?period={html.escape(period)}&start={html.escape(start)}&end={html.escape(end)}">View all sessions</a></div></section>"""
    return layout("Dashboard", "dashboard", body)


def ev_sessions(summary: dict[str, Any], period: str, start: str, end: str, message: str = "") -> str:
    ev = summary["ev"]
    rows = "".join(f'<tr><td>{html.escape(str(item["start_time"])[:16])}</td><td>{html.escape(str(item["end_time"])[:16])}</td><td>{item["duration_minutes"]:.0f} min</td><td>{energy(item["energy_kwh"])}</td><td>{money(item["cost"])}</td></tr>' for item in ev["sessions"])
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No imported Ocular sessions in this period.</td></tr>'
    notice = f'<div class="warning">{html.escape(message)}</div>' if message else ""
    body = f"""<h1>EV Sessions</h1><p class="subtle">Imported Ocular charging sessions are repriced from tariff history.</p>{notice}
<section class="metrics"><div class="metric"><span>EV charging</span><strong>{energy(ev["energy_kwh"])}</strong></div><div class="metric"><span>EV charging cost</span><strong>{money(ev["cost"])}</strong></div><div class="metric"><span>Sessions</span><strong>{len(ev["sessions"])}</strong></div></section>
<section class="panel"><h2>Import Ocular sessions</h2><form method="post" action="/ev/import" enctype="multipart/form-data"><label>Ocular export file<input type="file" name="import_file" accept=".csv,.xlsx,.xlsm" required></label><p class="muted">CSV, Excel, and macro-enabled Excel exports are accepted.</p><button type="submit">Import sessions</button></form></section>
<section class="panel"><h2>Session history</h2><table><thead><tr><th>Start</th><th>End</th><th>Duration</th><th>Energy</th><th>Calculated cost</th></tr></thead><tbody>{rows}</tbody></table></section>"""
    return layout("EV Sessions", "ev", body)


def settings(tariffs: list[dict[str, Any]], solaredge_configured: bool, timezone: str, message: str = "") -> str:
    rows = "".join(f'<tr><td>{html.escape(str(item.get("effective_from") or ""))}</td><td>{html.escape(str(item.get("effective_to") or "Current"))}</td><td>{html.escape(str(item.get("tariff_name") or ""))}<br><small>{html.escape(str(item.get("rates") or ""))}</small></td><td>{money(item.get("supply_charge_per_day"))}/day</td></tr>' for item in tariffs) or '<tr><td colspan="4" class="empty">No tariff history has been configured.</td></tr>'
    notice = f'<div class="warning">{html.escape(message)}</div>' if message else ""
    body = f"""<h1>Settings</h1>{notice}<section class="settings-grid"><div class="panel"><h2>Synergy tariff history</h2><table><thead><tr><th>From</th><th>To</th><th>Tariff</th><th>Supply</th></tr></thead><tbody>{rows}</tbody></table></div><div class="panel"><h2>Add tariff period</h2><form method="post" action="/settings/tariff/add"><label>Effective from<input type="date" name="effective_from" required></label><label>Plan<select name="plan_id"><option value="synergy_ev_add_on">Synergy EV Add On</option><option value="synergy_home_plan">Synergy Home Plan</option></select></label><input type="hidden" name="return_to" value="/settings"><button type="submit">Add tariff period</button></form><p class="muted">Adding a period preserves existing historical versions.</p></div></section><section class="settings-grid"><div class="panel"><h2>SolarEdge</h2><p>{"SolarEdge configuration is present." if solaredge_configured else "SolarEdge configuration has not been detected."}</p><p class="muted">Collection, recovery, and backfill remain available in the existing local collector.</p></div><div class="panel"><h2>General</h2><p>Timezone: {html.escape(timezone)}</p></div></section>"""
    return layout("Settings", "settings", body)
