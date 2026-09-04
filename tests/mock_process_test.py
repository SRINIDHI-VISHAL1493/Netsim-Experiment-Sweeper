"""Offline smoke test for the campaign engine; no NetSim installation required."""
import csv, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Campaign, run_campaign, element_catalog, metric_catalog

with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "campaigns"
    config = ROOT / "samples" / "Configuration.netsim"
    metrics = ROOT / "samples" / "Metrics.xml"
    params = element_catalog(config); metric = next(m for m in metric_catalog(metrics) if m["column"] == "Throughput")
    protocol = next(p for p in params if p["name"] == "protocol")
    campaign = Campaign(id="mock_test", name="Mock test", output=str(out), config=str(config), metrics=str(metrics), binary="", license_type="server", license_value="", max_runs=10, mock=True,
        parameters=[{"nodeId":protocol["id"], "label":"Protocol", "mode":"list", "values":"New_Reno,CUBIC"}],
        metric_specs=[{"metricId":metric["id"], "label":"Throughput"}])
    run_campaign(campaign)
    assert campaign.state == "Complete", campaign.state
    assert [r["status"] for r in campaign.runs] == ["Passed", "Passed"], campaign.runs
    csv_path = out / "mock_test" / "cumulative_results.csv"
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 2 and rows[0]["Throughput"] == "12.5", rows
    assert (out / "mock_test" / "run_0001" / "Configuration.netsim").exists()
print("PASS: 2 mock runs generated isolated configurations, metrics, and cumulative CSV.")
