"""Safe standalone HTML projection of a saved plan. No network or model calls."""
import argparse
import json
from html import escape
from pathlib import Path

from .discovery import discovery_plan

STYLE = """
:root{color-scheme:light;--ink:#173c3b;--muted:#526565;--line:#d8e1da;--paper:#faf9f5}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{font:16px/1.6 system-ui,sans-serif;margin:0;background:var(--paper);color:var(--ink)}
main{max-width:1080px;margin:auto;padding:32px 24px 80px}header{padding:28px 0 20px;border-bottom:1px solid var(--line)}
h1{font:clamp(32px,5vw,56px)/1.12 Georgia,serif;max-width:850px;margin:12px 0 20px}h2{font:28px/1.2 Georgia,serif}h3{font-size:18px;margin-bottom:8px}
p{margin:8px 0 16px}.eyebrow{font-size:12px;letter-spacing:2px;text-transform:uppercase;font-weight:700}
nav{display:flex;gap:8px;flex-wrap:wrap;padding:20px 0}a{color:var(--ink)}nav a{border:1px solid var(--line);border-radius:24px;padding:8px 16px;text-decoration:none;background:white}
a:hover,summary:hover{background:#e8efea}a:focus-visible,summary:focus-visible{outline:3px solid #a04e22;outline-offset:3px}
section{background:white;border:1px solid var(--line);border-radius:16px;padding:24px;margin:22px 0;scroll-margin-top:16px}
.banner{background:#fff1d2;border-left:4px solid #a55a1f;padding:14px 18px;border-radius:4px}.muted{color:var(--muted)}
.activity{border-top:1px solid var(--line);padding:12px 0}.tag{font-size:13px;padding:5px 10px;border-radius:20px;background:#eaf0eb;display:inline-block}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:16px 0}.metric{padding:12px;border-radius:8px;background:#f3f5ef}.metric dt{font-size:13px;color:var(--muted)}.metric dd{margin:5px 0 0;font-weight:700}
details{margin:16px 0}summary{cursor:pointer;padding:10px 0;font-weight:600}table{width:100%;border-collapse:collapse}td,th{padding:12px 8px;border-bottom:1px solid var(--line);text-align:left;overflow-wrap:anywhere}th{font-size:14px}
li{margin-bottom:8px}footer{font-size:14px;color:var(--muted)}
@media(max-width:600px){main{padding:16px 14px 40px}section{padding:18px}h2{font-size:24px}td,th{padding:8px 4px;font-size:14px}}
@media print{body{background:white}nav{display:none}main{max-width:none}section{break-inside:avoid;border-radius:0}details{display:block}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def has_sample_evidence(value):
    if isinstance(value, dict):
        return str(value.get("provider", "")).startswith("synthetic") or any(has_sample_evidence(v) for v in value.values())
    return isinstance(value, list) and any(has_sample_evidence(v) for v in value)


def render_plan(payload):
    view = discovery_plan(payload)
    def text(value):
        return escape(str(value)) if value is not None else "Unknown"
    def label(value):
        return text(str(value).replace("_", " ").capitalize()) if value is not None else "Unknown"
    def minutes(value):
        return "Unknown" if value is None else f"{value:g} min"
    summary = view["trip_summary"]
    sample = has_sample_evidence(payload)
    notice = 'Sample data · Portfolio demonstration. Hotel and transport amounts are test evidence, not live quotes.' if sample else 'Draft travel plan · Prices and availability need confirmation before booking.'
    if any(poi['name'] == 'Selected place (refresh details)' for day in view['days'] for poi in day['pois']):
        notice += ' Saved source details need refreshing; place names and explanations are incomplete in this view.'
    sections = [f'<header><p class="eyebrow">Your travel notebook</p><h1>{text(summary["title"])}</h1>',
        f'<p>{text(summary["start_date"])} → {text(summary["end_date"])} · {len(view["days"])} days · {text(summary["adults"])} adults · {text(summary["children"])} children</p>',
        f'<p>{text(summary["overview"])}</p><p class="banner">{notice}</p>',
        f'<p class="muted">Plan status: {label(view["source_status"])}. Visit durations and daily allowances are estimates unless explicitly sourced.</p></header>',
        '<nav aria-label="Itinerary sections">' + ''.join(f'<a href="#day-{d["day"]}">Day {d["day"]}</a>' for d in view["days"]) + '<a href="#stays">Stays</a><a href="#transfers">Transfers</a><a href="#costs">Budget</a><a href="#warnings">Before you go</a></nav>']
    for day in view["days"]:
        content = [f'<h2>Day {day["day"]} · {text(day["date"])} · {text(day["base_location"])}</h2>']
        for poi in day["pois"]:
            basis = {"planning_allowance": "Estimated visit allowance", "provider": "Source claim; see validation warnings", "unknown": "Visit time unconfirmed"}.get(poi["duration_basis"], "Visit time unconfirmed")
            content.append(f'<article class="activity"><h3>{text(poi["name"])}</h3><p>{text(poi["reason"])}</p><p class="tag">{text(minutes(poi["visit_minutes"]))} · {basis}</p></article>')
        if day["content_status"] == "pending":
            content.append('<p class="banner">Activities still need planning for this day.</p>')
        for event in day["other_events"]:
            content.append(f'<p><b>{text(event["kind"])}: {text(event["title"])}</b> — {text(event["reason"])}</p>')
        if day["workload"]:
            w = day["workload"]
            status = {"within_allowance": "Within planning allowance", "travel_only": "Travel-only day", "overloaded": "Needs fewer activities", "unknown": "Workload unverified"}.get(w["status"], w["status"])
            content.append(f'<p class="tag">{text(status)}</p><dl class="metrics">')
            fields = [("Active day", "envelope_minutes"), ("Time for visits", "poi_capacity_minutes"),
                      ("Planned visits", "visit_minutes"), ("City transfers", "transfer_minutes"),
                      ("Meals / rest" if "local_movement_minutes" in w else "Combined rest / movement buffer", "buffer_minutes")]
            fields += [(title, key) for title, key in [("Local movement", "local_movement_minutes"), ("Contingency", "contingency_minutes")] if key in w]
            content.extend(f'<div class="metric"><dt>{title}</dt><dd>{text(minutes(w.get(key)))}</dd></div>' for title, key in fields)
            content.append('</dl><details><summary>How this day was planned</summary><p>' + text(w.get("basis", "Saved planning allowances; actual durations remain unverified.")) + '</p></details>')
        else:
            content.append('<p class="muted">Daily time budget has not been checked.</p>')
        sections.append(f'<section id="day-{day["day"]}">' + ''.join(content) + '</section>')
    hotels = ['<h2>Where to stay</h2>']
    for h in view["accommodation"]:
        s, o = h["stay_request"], h["selected"] or {}
        hotels.append(f'<h3>{text(o.get("name"))}</h3><p>{text(s["check_in"])} → {text(s["check_out"])} · {s["adults"]} adults, {s["children"]} children · {s["rooms"]} room(s)</p><p>{text(o.get("currency"))} {text(o.get("price"))} · {label(o.get("price_basis"))} · availability {label(o.get("availability"))}</p><p>{text(h["reason"])}</p>')
    if not view["accommodation"]:
        hotels.append('<p>No stay recommendation available. Accommodation cost remains unconfirmed.</p>')
    sections.append('<section id="stays">' + ''.join(hotels) + '</section>')
    transfers = ['<h2>City transfers</h2>']
    for t in view["transport"]:
        o = t["selected"] or {}
        minutes = round(o["duration_s"] / 60) if o.get("duration_s") is not None else None
        transfers.append(f'<h3>{text(t["origin"])} → {text(t["destination"])} · {label(t["mode"])}</h3><p>{text(o.get("departure"))} → {text(o.get("arrival"))}</p><p>{text(minutes)} min · {text(o.get("fare_currency"))} {text(o.get("fare_amount"))} · {label(o.get("fare_scope"))}</p><p>{text(t["reason"])}</p>')
    if not view["transport"]:
        transfers.append('<p>No city-transfer recommendation available.</p>')
    sections.append('<section id="transfers">' + ''.join(transfers) + '</section>')
    weather = ['<h2>Recorded weather</h2><p>Saved source information, not a newly fetched forecast. Recheck before departure. Historical averages are not forecasts.</p>']
    if not view['weather']:
        weather.append('<p>Weather information unavailable.</p>')
    for record in view['weather']:
        weather.append(f'<h3>{text(record["location"])}</h3><p>Status: {label(record["status"])}</p>')
        report = record.get('report') or {}
        forecasts = report.get('forecast_days', [])
        if not forecasts and record.get('date'):
            forecasts = [record]
        for forecast in forecasts:
            weather.append(f'<p>{text(forecast["date"])}: {text(forecast.get("min_c"))} to {text(forecast.get("max_c"))} °C; precipitation probability {text(forecast.get("precipitation_probability"))}%.</p>')
        history = report.get('historical_summary')
        if history:
            weather.append(f'<p>Historical estimate ({text(history["start_date"])} → {text(history["end_date"])}): typical {text(history["typical_min_c"])} to {text(history["typical_max_c"])} °C; wet-day frequency {text(history["wet_day_frequency_pct"])}%.</p>')
        if not forecasts and not history:
            weather.append('<p>Weather information unavailable.</p>')
        for unavailable in report.get('unavailable_dates', []):
            weather.append(f'<p>{text(unavailable)}: weather unavailable.</p>')
        for warning in record['warnings']:
            weather.append(f'<p>{text(warning)}</p>')
        for evidence in record['evidence']:
            weather.append(f'<p class="muted">Source: {text(evidence.get("provider"))}; retrieved: {text(evidence.get("retrieved_at"))}.</p>')
    sections.append('<section id="weather">' + ''.join(weather) + '</section>')
    profiles = ['<h2>Transport comparisons</h2><p>Compared using available offers for the planned city order. Missing offers can change the recommendation.</p><table><tr><th>Option</th><th>Travel minutes</th><th>Cost</th></tr>']
    for p in view["route_profiles"].get("profiles", []):
        c = p.get("cost", {})
        profiles.append(f'<tr><td>{text(p["profile"])}</td><td>{text(p.get("travel_minutes"))}</td><td>{text(c.get("currency"))} {text(c.get("total_low"))}</td></tr>')
    profiles.append('</table>')
    if view["route_profiles"].get("profiles"):
        sections.append('<section>' + ''.join(profiles) + '</section>')
    c = view["cost"]
    costs = ['<h2>Cost breakdown</h2><p>Subtotal covers only priced items. Unknown expenses are additional; this is not a final trip quote.</p><table><tr><th>Category</th><th>Amount</th></tr>']
    for category, amount in c["category_totals"].items():
        costs.append(f'<tr><td>{label(category)}</td><td>{text(c["currency"])} {text(amount)}</td></tr>')
    for category in c["unknown_components"]:
        costs.append(f'<tr><td>{label(category)}</td><td>Unknown — excluded from subtotal</td></tr>')
    costs.append(f'<tr><th>Known subtotal</th><th>{text(c["currency"])} {text(c["known_subtotal"])}</th></tr></table><p>Trip budget: {text(summary["budget"])} {text(summary["currency"])} · budget status: {text(c["budget_status"])}</p>')
    sections.append('<section id="costs">' + ''.join(costs) + '</section>')
    warnings = list(dict.fromkeys(w["message"] for w in view["warnings"]))
    sections.append('<section id="warnings"><h2>Confirm before travelling</h2><ul>' + ''.join('<li>' + text(w) + '</li>' for w in warnings) + '</ul><p>Recheck opening hours, entry requirements, accessibility and bookings before departure.</p></section>')
    sections.append('<footer>Keep this plan with you: use your browser’s Print / Save as PDF. No bookings have been made.</footer>')
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + text(summary['title']) + ' · Trip planner</title><style>' + STYLE + '</style></head><body><main>' + ''.join(sections) + '</main></body></html>'


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("saved_plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(render_plan(json.loads(args.saved_plan.read_text(encoding="utf-8"))), encoding="utf-8")
