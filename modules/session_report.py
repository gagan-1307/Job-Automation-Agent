'''
Session report generator for Auto Job Applier.

Produces a self-contained HTML dashboard saved to the logs folder at the
end of every bot run. Open it in any browser - no server required.

Public interface (called from runAiBot.py):
    SessionReport               - collector: add jobs and metadata as the run progresses
    SessionReport.save(path)    - write the final HTML dashboard to disk
'''

from __future__ import annotations

import json
import html
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from modules.job_state import JobState, JobStateMachine, _STATE_LABEL


# ---------------------------------------------------------------------------
# Palette - maps each terminal / current state to a CSS colour token
# ---------------------------------------------------------------------------
_STATE_COLOR = {
    "DISCOVERED":        "#6b7280",   # gray
    "MATCHED":           "#3b82f6",   # blue
    "PREPARING":         "#a855f7",   # purple
    "NEEDS_INPUT":       "#f59e0b",   # amber
    "AWAITING_APPROVAL": "#f97316",   # orange
    "SUBMITTED":         "#10b981",   # emerald
    "VERIFIED":          "#22c55e",   # green
    "SKIPPED":           "#64748b",   # slate
    "FAILED":            "#ef4444",   # red
}

_FUNNEL_STATES = [
    "DISCOVERED", "MATCHED", "PREPARING",
    "AWAITING_APPROVAL", "SUBMITTED", "VERIFIED",
]


# ---------------------------------------------------------------------------
# Per-job record
# ---------------------------------------------------------------------------
@dataclass
class JobRecord:
    job_id:    str
    title:     str
    company:   str
    sm:        JobStateMachine
    search_term: str = "Unknown"
    # optional enrichment
    work_location: str = ""
    work_style:    str = ""
    experience_required: str = ""


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------
class SessionReport:
    '''
    Accumulate per-job state machines during a run, then call save() once at
    the end to get a self-contained HTML dashboard.

    Usage in runAiBot.py
    --------------------
    session = SessionReport()
    ...
    sm = JobStateMachine(job_id, title, company)
    session.add(JobRecord(job_id, title, company, sm, search_term))
    ...
    session.save("logs/session_2026-10-11.html")
    '''

    def __init__(self) -> None:
        self._records: list[JobRecord] = []
        self._started: float = time.monotonic()
        self._start_dt: datetime = datetime.now()
        self.run_cycles: int = 0

    def add(self, record: JobRecord) -> None:
        self._records.append(record)

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------
    def _counts_by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {k: 0 for k in _STATE_LABEL.values()}
        for r in self._records:
            lbl = _STATE_LABEL[r.sm.state]
            counts[lbl] = counts.get(lbl, 0) + 1
        return counts

    def _counts_by_search_term(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._records:
            counts[r.search_term] = counts.get(r.search_term, 0) + 1
        return counts

    def _funnel_counts(self) -> dict[str, int]:
        '''
        For each funnel stage, count jobs that REACHED that stage or beyond
        (i.e. have that state in their history or ARE in that state).
        '''
        def reached(record: JobRecord, target_label: str) -> bool:
            target = next(s for s, l in _STATE_LABEL.items() if l == target_label)
            if record.sm.state == target:
                return True
            return any(e.from_state == target or e.to_state == target
                       for e in record.sm.history)

        return {stage: sum(1 for r in self._records if reached(r, stage))
                for stage in _FUNNEL_STATES}

    def _wall_seconds(self) -> float:
        return time.monotonic() - self._started

    # ------------------------------------------------------------------
    # HTML renderer
    # ------------------------------------------------------------------
    def save(self, path: str) -> str:
        '''Render and write the dashboard. Returns the resolved path.'''
        counts   = self._counts_by_state()
        funnel   = self._funnel_counts()
        by_term  = self._counts_by_search_term()
        total    = len(self._records)
        verified = counts.get("VERIFIED", 0)
        skipped  = counts.get("SKIPPED", 0)
        failed   = counts.get("FAILED", 0)
        submitted_n = counts.get("SUBMITTED", 0) + verified
        wall     = self._wall_seconds()
        time_saved_min = round((verified * 80 + skipped * 10 + failed * 5) / 60)

        html_content = _TEMPLATE.format(
            session_start    = self._start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            wall_time        = _fmt_duration(wall),
            run_cycles       = self.run_cycles,
            total            = total,
            verified         = verified,
            submitted        = submitted_n,
            skipped          = skipped,
            failed           = failed,
            time_saved       = time_saved_min,
            funnel_json      = json.dumps([funnel.get(s, 0) for s in _FUNNEL_STATES]),
            funnel_labels    = json.dumps(_FUNNEL_STATES),
            funnel_colors    = json.dumps([_STATE_COLOR[s] for s in _FUNNEL_STATES]),
            donut_labels     = json.dumps(list(counts.keys())),
            donut_data       = json.dumps(list(counts.values())),
            donut_colors     = json.dumps([_STATE_COLOR.get(k, "#6b7280") for k in counts.keys()]),
            term_labels      = json.dumps(list(by_term.keys())),
            term_data        = json.dumps(list(by_term.values())),
            term_colors      = json.dumps([
                "#3b82f6", "#a855f7", "#10b981", "#f59e0b",
                "#ef4444", "#f97316", "#06b6d4", "#ec4899",
            ][:len(by_term)]),
            jobs_table_rows  = _render_rows(self._records),
        )

        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html_content)
        return os.path.abspath(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _render_rows(records: list[JobRecord]) -> str:
    rows = []
    for r in records:
        state_lbl = _STATE_LABEL[r.sm.state]
        color     = _STATE_COLOR.get(state_lbl, "#6b7280")
        trail     = " -> ".join(_STATE_LABEL[e.from_state] for e in r.sm.history)
        if r.sm.history:
            trail += " -> " + state_lbl
        else:
            trail = state_lbl
        last_reason = r.sm.history[-1].reason if r.sm.history else ""
        elapsed = _fmt_duration(r.sm.elapsed_seconds())
        rows.append(
            f'''<tr>
              <td class="mono">{html.escape(r.job_id)}</td>
              <td><span class="job-title">{html.escape(r.title)}</span></td>
              <td>{html.escape(r.company)}</td>
              <td><span class="badge" style="background:{color}22;color:{color};border-color:{color}44">{state_lbl}</span></td>
              <td class="mono dim">{elapsed}</td>
              <td class="dim small">{html.escape(last_reason)}</td>
              <td class="dim small trail">{html.escape(trail)}</td>
            </tr>'''
        )
    return "\n".join(rows) if rows else '<tr><td colspan="7" class="empty">No jobs processed yet.</td></tr>'


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Job Applier Session Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:       #0c0e14;
    --surface:  #13161f;
    --surface2: #1a1e2a;
    --border:   #252836;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --green:    #22c55e;
    --blue:     #3b82f6;
    --amber:    #f59e0b;
    --red:      #ef4444;
    --purple:   #a855f7;
    --orange:   #f97316;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }}
  /* ---- HEADER ---- */
  .header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #0f172a 100%);
    border-bottom: 1px solid var(--border);
    padding: 28px 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }}
  .header-left h1 {{
    font-size: 22px;
    font-weight: 700;
    background: linear-gradient(135deg, #60a5fa, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.5px;
  }}
  .header-left p {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .header-meta {{
    display: flex; gap: 20px; flex-wrap: wrap;
  }}
  .meta-chip {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 14px;
    font-size: 12px;
    color: var(--muted);
  }}
  .meta-chip strong {{ color: var(--text); font-weight: 600; }}
  /* ---- MAIN ---- */
  .main {{ padding: 32px 40px; max-width: 1600px; margin: 0 auto; }}
  /* ---- STAT CARDS ---- */
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px 20px;
    position: relative;
    overflow: hidden;
    transition: transform .2s, box-shadow .2s;
  }}
  .stat-card:hover {{ transform: translateY(-3px); }}
  .stat-card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: var(--accent);
    border-radius: 14px 14px 0 0;
  }}
  .stat-card .icon {{
    font-size: 28px;
    margin-bottom: 12px;
    display: block;
  }}
  .stat-card .value {{
    font-size: 36px;
    font-weight: 700;
    color: var(--accent);
    line-height: 1;
  }}
  .stat-card .label {{
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .8px;
    margin-top: 6px;
  }}
  .card-green  {{ --accent: var(--green); }}
  .card-blue   {{ --accent: var(--blue); }}
  .card-amber  {{ --accent: var(--amber); }}
  .card-red    {{ --accent: var(--red); }}
  .card-purple {{ --accent: var(--purple); }}
  .card-orange {{ --accent: var(--orange); }}
  /* ---- FUNNEL ---- */
  .section-title {{
    font-size: 15px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .section-title::before {{
    content: '';
    display: inline-block;
    width: 4px; height: 16px;
    background: linear-gradient(180deg, #60a5fa, #a78bfa);
    border-radius: 2px;
  }}
  .funnel-container {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    margin-bottom: 24px;
    overflow-x: auto;
  }}
  .funnel-track {{
    display: flex;
    align-items: center;
    gap: 0;
    min-width: 700px;
  }}
  .funnel-node {{
    flex: 1;
    text-align: center;
    position: relative;
  }}
  .funnel-node .fn-box {{
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    min-width: 110px;
    transition: border-color .2s;
  }}
  .funnel-node .fn-box:hover {{ border-color: var(--fn-color); }}
  .fn-count {{
    font-size: 26px;
    font-weight: 700;
    color: var(--fn-color);
  }}
  .fn-label {{
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .7px;
  }}
  .fn-bar {{
    width: 80%;
    height: 4px;
    border-radius: 2px;
    background: var(--fn-color);
    opacity: .6;
    margin-top: 4px;
  }}
  .funnel-arrow {{
    color: var(--muted);
    font-size: 18px;
    padding: 0 4px;
    flex-shrink: 0;
  }}
  /* ---- CHARTS ROW ---- */
  .charts-row {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 28px;
  }}
  @media (max-width: 900px) {{ .charts-row {{ grid-template-columns: 1fr; }} }}
  .chart-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
  }}
  .chart-card canvas {{ max-height: 240px; }}
  /* ---- TABLE ---- */
  .table-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    overflow-x: auto;
  }}
  .search-bar {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }}
  .search-bar input {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 14px;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
    width: 260px;
    outline: none;
    transition: border-color .2s;
  }}
  .search-bar input:focus {{ border-color: #3b82f6; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  th {{
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .8px;
    color: var(--muted);
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }}
  th:hover {{ color: var(--text); }}
  td {{
    padding: 11px 12px;
    border-bottom: 1px solid #1a1e2a;
    vertical-align: top;
  }}
  tr:hover td {{ background: var(--surface2); }}
  .badge {{
    display: inline-block;
    padding: 2px 9px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .4px;
    border: 1px solid transparent;
  }}
  .mono {{ font-family: 'JetBrains Mono', monospace; }}
  .dim {{ color: var(--muted); }}
  .small {{ font-size: 12px; }}
  .trail {{ max-width: 300px; word-break: break-all; }}
  .job-title {{ font-weight: 500; }}
  .empty {{ text-align: center; padding: 40px; color: var(--muted); }}
  /* ---- FOOTER ---- */
  .footer {{
    text-align: center;
    padding: 28px;
    color: var(--muted);
    font-size: 12px;
    border-top: 1px solid var(--border);
    margin-top: 40px;
  }}
  /* ---- ANIMATIONS ---- */
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(16px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .stat-card, .funnel-container, .chart-card, .table-card {{
    animation: fadeUp .4s ease both;
  }}
  .stat-card:nth-child(1) {{ animation-delay: .05s; }}
  .stat-card:nth-child(2) {{ animation-delay: .10s; }}
  .stat-card:nth-child(3) {{ animation-delay: .15s; }}
  .stat-card:nth-child(4) {{ animation-delay: .20s; }}
  .stat-card:nth-child(5) {{ animation-delay: .25s; }}
  .stat-card:nth-child(6) {{ animation-delay: .30s; }}
</style>
</head>
<body>

<!-- HEADER -->
<header class="header">
  <div class="header-left">
    <h1>&#128196; Job Applier Session Report</h1>
    <p>Auto-generated at session end &mdash; {session_start}</p>
  </div>
  <div class="header-meta">
    <div class="meta-chip">Duration <strong>{wall_time}</strong></div>
    <div class="meta-chip">Cycles <strong>{run_cycles}</strong></div>
    <div class="meta-chip">Total jobs processed <strong>{total}</strong></div>
  </div>
</header>

<!-- MAIN -->
<div class="main">

  <!-- STAT CARDS -->
  <div class="stat-grid">
    <div class="stat-card card-green">
      <span class="icon">&#9989;</span>
      <div class="value">{verified}</div>
      <div class="label">Verified Applied</div>
    </div>
    <div class="stat-card card-blue">
      <span class="icon">&#128228;</span>
      <div class="value">{submitted}</div>
      <div class="label">Submitted</div>
    </div>
    <div class="stat-card card-amber">
      <span class="icon">&#9940;</span>
      <div class="value">{skipped}</div>
      <div class="label">Skipped</div>
    </div>
    <div class="stat-card card-red">
      <span class="icon">&#10060;</span>
      <div class="value">{failed}</div>
      <div class="label">Failed</div>
    </div>
    <div class="stat-card card-purple">
      <span class="icon">&#128203;</span>
      <div class="value">{total}</div>
      <div class="label">Total Processed</div>
    </div>
    <div class="stat-card card-orange">
      <span class="icon">&#9200;</span>
      <div class="value">{time_saved}m</div>
      <div class="label">Est. Time Saved</div>
    </div>
  </div>

  <!-- STATE FUNNEL -->
  <div class="section-title">Application Funnel</div>
  <div class="funnel-container">
    <div class="funnel-track" id="funnelTrack"></div>
  </div>

  <!-- CHARTS -->
  <div class="charts-row">
    <div class="chart-card">
      <div class="section-title">State Breakdown</div>
      <canvas id="donutChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="section-title">Jobs per Search Term</div>
      <canvas id="barChart"></canvas>
    </div>
  </div>

  <!-- JOBS TABLE -->
  <div class="table-card">
    <div class="section-title">All Jobs This Session</div>
    <div class="search-bar">
      <input type="text" id="searchInput" placeholder="&#128269;  Filter by title, company, state..."/>
    </div>
    <table id="jobsTable">
      <thead>
        <tr>
          <th onclick="sortTable(0)">Job ID</th>
          <th onclick="sortTable(1)">Title</th>
          <th onclick="sortTable(2)">Company</th>
          <th onclick="sortTable(3)">State</th>
          <th onclick="sortTable(4)">Elapsed</th>
          <th onclick="sortTable(5)">Reason</th>
          <th onclick="sortTable(6)">Trail</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        {jobs_table_rows}
      </tbody>
    </table>
  </div>
</div>

<!-- FOOTER -->
<footer class="footer">
  Auto Job Applier &mdash; Session ended {session_start} &mdash; {wall_time} total runtime
</footer>

<script>
// ---- Build funnel ----
const funnelLabels = {funnel_labels};
const funnelCounts = {funnel_json};
const funnelColors = {funnel_colors};
const track = document.getElementById('funnelTrack');
const maxCount = Math.max(...funnelCounts, 1);
funnelLabels.forEach((label, i) => {{
  const node = document.createElement('div');
  node.className = 'funnel-node';
  const pct = Math.round((funnelCounts[i] / maxCount) * 100);
  node.innerHTML = `
    <div class="fn-box" style="--fn-color:${{funnelColors[i]}}">
      <div class="fn-count" style="color:${{funnelColors[i]}}">${{funnelCounts[i]}}</div>
      <div class="fn-bar" style="background:${{funnelColors[i]}};width:${{pct}}%"></div>
      <div class="fn-label">${{label}}</div>
    </div>`;
  track.appendChild(node);
  if (i < funnelLabels.length - 1) {{
    const arrow = document.createElement('div');
    arrow.className = 'funnel-arrow';
    arrow.textContent = '>';
    track.appendChild(arrow);
  }}
}});

// ---- Donut chart ----
new Chart(document.getElementById('donutChart'), {{
  type: 'doughnut',
  data: {{
    labels: {donut_labels},
    datasets: [{{ data: {donut_data}, backgroundColor: {donut_colors}, borderWidth: 2, borderColor: '#13161f' }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{
      legend: {{ position: 'right', labels: {{ color: '#94a3b8', font: {{ family: 'Inter', size: 12 }}, padding: 14, boxWidth: 12 }} }},
      tooltip: {{ callbacks: {{ label: ctx => ` ${{ctx.label}}: ${{ctx.parsed}}` }} }}
    }},
    cutout: '62%'
  }}
}});

// ---- Bar chart ----
new Chart(document.getElementById('barChart'), {{
  type: 'bar',
  data: {{
    labels: {term_labels},
    datasets: [{{ label: 'Jobs', data: {term_data}, backgroundColor: {term_colors}, borderRadius: 6, borderSkipped: false }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: true, indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ color: '#1a1e2a' }}, ticks: {{ color: '#64748b' }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ color: '#94a3b8' }} }}
    }}
  }}
}});

// ---- Table search ----
document.getElementById('searchInput').addEventListener('input', function() {{
  const q = this.value.toLowerCase();
  document.querySelectorAll('#tableBody tr').forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}});

// ---- Table sort ----
let _sortDir = {{}};
function sortTable(col) {{
  const tbody = document.getElementById('tableBody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  const dir   = (_sortDir[col] = !_sortDir[col]);
  rows.sort((a, b) => {{
    const at = a.cells[col]?.textContent.trim() || '';
    const bt = b.cells[col]?.textContent.trim() || '';
    return dir ? at.localeCompare(bt) : bt.localeCompare(at);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>
'''
