import os
import sys
import textwrap
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], textColor=colors.HexColor("#1F3A5F"), fontSize=14, spaceBefore=10)
B = ParagraphStyle("B", parent=ss["BodyText"], fontSize=10, leading=14)
CELL = ParagraphStyle("c", parent=B, fontSize=9, leading=12)
NOTE = ParagraphStyle("N", parent=B, backColor=colors.HexColor("#FFF4D6"), borderPadding=6, spaceBefore=6, spaceAfter=8)
MONO = ParagraphStyle("m", parent=ss["Code"], fontSize=6.8, leading=8.6, backColor=colors.HexColor("#F4F6FA"), borderPadding=5)
S = []
def p(t, st=B): S.append(Paragraph(t, st)); S.append(Spacer(1, 5))
def h(t): S.append(Paragraph(t, H1))
def c(t): return Paragraph(t, CELL)
def tbl(rows, widths):
    t = Table([[c(x) for x in r] for r in rows], colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#DCE8F7")), ("GRID", (0,0), (-1,-1), 0.4, colors.grey), ("VALIGN", (0,0), (-1,-1), "TOP")]))
    S.append(t); S.append(Spacer(1, 8))
def img(name, ratio): S.append(Image(os.path.join(ROOT, "docs", name), width=16.5*cm, height=16.5*cm*ratio)); S.append(Spacer(1, 6))

S.append(Paragraph("Serverless Monitoring &amp; Alerting Pipeline", ParagraphStyle("T", parent=ss["Title"], textColor=colors.HexColor("#1F3A5F"))))
p("<b>AWS Lambda, CloudWatch, Python</b><br/>Author: Umesh Chandra Namani")
p("<b>About this report:</b> it explains the design and what the repository contains. It has not been deployed to AWS while preparing it, and the "
  "claim that incident response became about a third faster is <b>not</b> made here: the project includes a tool to measure it from real incidents. "
  "The sample alert below is the real output of the alert router run on synthetic data.", NOTE)

h("1. The idea in simple words")
p("When something breaks in a system, the first minutes are lost looking for what broke. This pipeline watches your logs and API traffic all the time, "
  "turns them into numbers that CloudWatch can graph and alarm on, checks that your web addresses are up, and when something goes wrong it sends "
  "the right person one message that already contains the likely cause: the most common errors, examples of failing requests with links to their traces, "
  "and the runbook. It also keeps a record of every incident, so you can measure how fast problems are really found and fixed.")
img("architecture.png", 6.8/12.5)

h("2. The pieces")
tbl([["Part", "What it does"],
     ["log-ingest (Lambda)", "Reads application logs (JSON or plain text) as they arrive. Counts errors/warnings per minute, keeps latency distributions, notes the most frequent errors, and raises an instant event for critical lines such as timeouts or out-of-memory."],
     ["api-ingest (Lambda)", "Reads API Gateway access logs. Per API and per route: request counts, 2xx/4xx/5xx, slow requests, and latency distributions. Remembers examples of failed or slow requests with their X-Ray trace id."],
     ["health-probe (Lambda)", "Every minute calls your URLs in parallel and records up/down, response time and days until the TLS certificate expires."],
     ["alert-router (Lambda)", "Receives alarm changes and critical-log events. Avoids duplicate pages, tracks each incident from open to recovery, adds context, and sends email (SNS) and optionally Slack."],
     ["CloudFormation + generator", "Creates the pipeline, a pipeline-health dashboard, and (from a small config file) alarms and a dashboard for your own services."]],
    [4.0*cm, 12.8*cm])
p("Metrics use CloudWatch's Embedded Metric Format: the functions just print JSON and CloudWatch creates the metrics, so no extra API calls or permissions are needed. "
  "Each metric is stamped with the minute the event happened, so graphs and alarms match reality.")

h("3. What a responder receives")
p("This is real output from the alert router, run on synthetic traffic with a simulated outage:")
def sample_alert():
    """The P1 page from the saved simulator run (regenerate with: python tools/simulate.py > docs/simulation_output.txt)."""
    path = os.path.join(ROOT, "docs", "simulation_output.txt")
    if not os.path.exists(path):
        return "(run tools/simulate.py to generate a sample alert)"
    text = open(path).read()
    start = text.index("subject: [P1] ALARM")
    block = text[text.index("\n", start) + 1:].split("\n\n--- ")[0]
    lines = []
    for line in block.splitlines():
        lines.extend(textwrap.wrap(line, 100, subsequent_indent="        ", break_long_words=True, break_on_hyphens=False) or [""])
    return "\n".join(lines)


S.append(Preformatted(sample_alert(), MONO)); S.append(Spacer(1, 8))
img("incident_flow.png", 4.6/12)
p("Repeated alarms for the same problem do not page again; a problem that comes back within 10 minutes reopens the same incident; "
  "a failed page is retried rather than lost; and critical log lines page immediately without waiting for an alarm to evaluate.")

h("4. Alarms and dashboards for your services")
p("Per service: error volume. Per API: 5xx <i>rate</i> (ignored when traffic is too low, so a quiet API does not page for one error) and p95 latency. "
  "Per probe: availability (no data counts as down) and certificate expiry. A small config file generates all of it, including one overview dashboard. "
  "Tests check that every metric the alarms and dashboards use is really produced by the code.")

h("5. Measuring incident response")
p("Each incident is stored with the time it opened, when the page was delivered, and when it recovered. <font face='Courier'>scripts/incident_report.py</font> reports mean, median "
  "and p90 time to recover and time to notify, and can compare with a CSV of your incidents from before the pipeline (opened_at, resolved_at). "
  "It warns when there are too few incidents to trust. Compare like with like and state what you compared.")

h("6. What was verified")
tbl([["Item", "Status"],
     ["Lambda logic, router, incident ledger, scripts, generator", "Run and tested: 115 automated tests using fake AWS clients"],
     ["Probes", "Tested against a real local HTTP server (success, 500, slow, timeout, refused)"],
     ["Alarms and dashboards reference only metrics the code emits", "Tested"],
     ["CloudFormation structure", "Checked (references, conditions, handlers, env vars, dashboard JSON); CI also runs cfn-lint and sam validate"],
     ["Deployment, real boto3 calls, real alarm evaluation, email/Slack delivery", "Not run (no AWS access); expect small fixes on first deploy"],
     ["Incident response shortened by about a third", "Not measured; use the report tool on your data"]],
    [7.0*cm, 9.8*cm])

h("7. Limits and next steps")
p("A metric usually becomes visible to alarms 1-2 minutes after the event, so use the instant critical-log path for urgent cases. CloudWatch allows two subscription filters per log group. "
  "The Slack webhook sits in a function environment variable (use Secrets Manager in production). Next steps: multi-account aggregation, composite and anomaly alarms, "
  "PagerDuty/Opsgenie, grouping related alarms into one incident, OpenTelemetry.")
SimpleDocTemplate(os.path.join(ROOT, "docs", "Monitoring_Report.pdf"), pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
                  title="Serverless Monitoring & Alerting Pipeline - Report", author="Umesh Chandra Namani").build(S)
