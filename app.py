#!/usr/bin/env python3
"""NetSim Experiment Sweeper - offline local web application.

Run `python app.py`, then open http://127.0.0.1:8765.  The application keeps
every campaign in its output folder and never overwrites the uploaded baseline.
"""
from __future__ import annotations
import csv, itertools, json, os, shutil, subprocess, sys, threading, time, uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workspace"; UPLOADS = WORK / "uploads"
INDEX = WORK / "campaign_index.json"
for p in (WORK, UPLOADS): p.mkdir(parents=True, exist_ok=True)

def stamp(): return datetime.now().isoformat(timespec="seconds")
def clean_tag(tag): return tag.split("}")[-1]
def xml_error(path):
    try: ET.parse(path); return None
    except ET.ParseError as e: return str(e)

def element_catalog(path):
    """Returns editable text leaves and attributes with stable positional IDs."""
    root = ET.parse(path).getroot(); rows=[]; n=0
    def visit(el, path_text, occurrence):
        nonlocal n
        here=f"{path_text}/{clean_tag(el.tag)}[{occurrence}]"
        children=list(el); counts={}
        for name, value in el.attrib.items():
            n+=1; rows.append({"id":f"a{n}","kind":"attribute","name":name,"value":value,"path":here+f"/@{name}","occurrence":occurrence,"tag":clean_tag(el.tag)})
        if not children and (el.text or "").strip():
            n+=1; rows.append({"id":f"e{n}","kind":"element","name":clean_tag(el.tag),"value":el.text.strip(),"path":here,"occurrence":occurrence,"tag":clean_tag(el.tag)})
        for child in children:
            t=clean_tag(child.tag); counts[t]=counts.get(t,0)+1; visit(child, here, counts[t])
    visit(root, "", 1); return rows

def find_target(root, stable_id):
    # IDs mirror element_catalog traversal exactly.
    n=0; answer=None
    def visit(el):
        nonlocal n, answer
        for name in el.attrib:
            n+=1
            if stable_id==f"a{n}": answer=(el, name, True); return
        if not list(el) and (el.text or "").strip():
            n+=1
            if stable_id==f"e{n}": answer=(el, None, False); return
        for c in list(el):
            if answer is None: visit(c)
    visit(root); return answer

def metric_catalog(path):
    root=ET.parse(path).getroot(); out=[]; n=0
    def visit(el, trail):
        nonlocal n
        children=list(el); tag=clean_tag(el.tag)
        fields={clean_tag(k):v for k,v in el.attrib.items()}
        for c in children:
            if not list(c) and (c.text or "").strip(): fields[clean_tag(c.tag)]=c.text.strip()
        for attr, value in el.attrib.items():
            n+=1; out.append({"id":f"m{n}","menu":clean_tag(root.tag),"table":trail[-1] if trail else tag,"row":tag,"column":"@"+clean_tag(attr),"value":value,"rowFields":fields})
        if not children and (el.text or "").strip():
            n+=1; out.append({"id":f"m{n}","menu":clean_tag(root.tag),"table":trail[-1] if trail else tag,"row":tag,"column":tag,"value":el.text.strip(),"rowFields":fields})
        for c in children: visit(c, trail+[tag])
    visit(root, [])
    # Row identifiers deliberately exclude the selected output column: values
    # naturally change between runs, while IDs/source/destination remain useful.
    for m in out:
        m["selector"]={k:v for k,v in m["rowFields"].items() if k != m["column"].lstrip("@")}
    return out

def resolve_metric(path, spec):
    """Resolve by table/row/column plus selected row-identifying fields.
    Returns (metric, error); duplicate matches are intentionally rejected.
    """
    reference_id=spec.get("metricId")
    reference=next((m for m in metric_catalog(spec["referencePath"]) if m["id"]==reference_id), None) if spec.get("referencePath") else None
    if not reference: return None, "Metric definition was not found in the reference Metrics.xml"
    selector=spec.get("selector", reference.get("selector",{}))
    candidates=[m for m in metric_catalog(path) if m["table"]==reference["table"] and m["row"]==reference["row"] and m["column"]==reference["column"] and all(str(m["rowFields"].get(k,""))==str(v) for k,v in selector.items())]
    if len(candidates)==1: return candidates[0], None
    if len(candidates)>1: return None, f"Metric '{spec['label']}' is ambiguous: {len(candidates)} rows match its identifier fields"
    return None, f"Metric '{spec['label']}' was not found in this run's Metrics.xml"

def values_for(spec):
    if spec.get("mode")=="range":
        try: start=float(spec["start"]); stop=float(spec["stop"]); step=float(spec["step"])
        except (ValueError, TypeError): raise ValueError(f"{spec.get('label','Parameter')}: numeric range is invalid")
        if step==0 or (stop-start)*step<0: raise ValueError(f"{spec.get('label','Parameter')}: range direction and step do not agree")
        values=[]; x=start; limit=0
        while (step>0 and x<=stop+1e-12) or (step<0 and x>=stop-1e-12):
            values.append(f"{x:g}"); x+=step; limit+=1
            if limit>100000: raise ValueError("Range is too large")
        return values
    raw = spec.get("values", "")
    source = raw if isinstance(raw, list) else str(raw).split(",")
    vals=[str(x).strip() for x in source if str(x).strip()]
    if not vals: raise ValueError(f"{spec.get('label','Parameter')}: provide at least one value")
    return vals

@dataclass
class Campaign:
    id: str; name: str; output: str; config: str; metrics: str|None; binary: str; license_type: str; license_value: str
    parameters: list; metric_specs: list; max_runs: int=500; mock: bool=False
    cancel: bool=False; state: str="Pending"; started: str=""; ended: str=""; runs: list=field(default_factory=list); log: list=field(default_factory=list)

CAMPAIGNS={}; PROCESSES={}; LOCK=threading.Lock()

def write_campaign(c):
    folder=Path(c.output)/c.id; folder.mkdir(parents=True, exist_ok=True)
    (folder/"campaign.json").write_text(json.dumps(asdict(c), indent=2), encoding="utf-8")
    known=json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    known[c.id]=str(folder); INDEX.write_text(json.dumps(known,indent=2),encoding="utf-8")
    return folder

def run_campaign(c):
    c.state="Running"; c.started=stamp(); campaign_dir=write_campaign(c); c.log.append(f"{c.started} Campaign started")
    for spec in c.metric_specs: spec.setdefault("referencePath", c.metrics)
    parameter_values=[values_for(p) for p in c.parameters]
    combos=list(itertools.product(*parameter_values))
    # Pre-create every run, so the interface and CSV can show Pending work.
    if not c.runs:
        c.runs=[{"run":i,"status":"Pending","inputs":dict(zip([p['label'] for p in c.parameters], combo)),"start":"","end":"","duration":"","output_folder":str(campaign_dir/f"run_{i:04d}"),"metrics":{},"error":""} for i,combo in enumerate(combos,1)]
        write_results(c,campaign_dir); write_campaign(c)
    for index, combo in enumerate(combos, 1):
        if c.cancel:
            for future in c.runs[index-1:]:
                if future["status"]=="Pending": future.update(status="Cancelled",error="Campaign cancelled",end=stamp())
            break
        run_dir=campaign_dir/f"run_{index:04d}"; run_dir.mkdir(exist_ok=True); started=stamp()
        record=c.runs[index-1]; record.update(status="Running",start=started); c.log.append(f"{stamp()} Run {index}/{len(combos)}: {record['inputs']}")
        try:
            dest=run_dir/"Configuration.netsim"; shutil.copy2(c.config,dest)
            tree=ET.parse(dest); root=tree.getroot()
            for spec, value in zip(c.parameters, combo):
                target=find_target(root,spec["nodeId"])
                if not target: raise RuntimeError(f"Target {spec['nodeId']} was not found in generated configuration")
                el, attr, is_attr=target
                if is_attr: el.set(attr,value)
                else: el.text=value
            tree.write(dest, encoding="utf-8", xml_declaration=True)
            if c.mock:
                fixture=ROOT/"samples"/"Metrics.xml"
                shutil.copy2(fixture,run_dir/"Metrics.xml")
                time.sleep(.15)
                proc_text="Mock NetSimCore completed successfully."
                (run_dir/"process.log").write_text(proc_text,encoding="utf-8")
            else:
                exe=Path(c.binary)/"NetSimCore.exe"
                if not exe.is_file(): raise RuntimeError("NetSimCore.exe was not found in the selected binary folder")
                cmd=[str(exe),"-apppath",c.binary,"-iopath",str(run_dir),"-license",c.license_value]
                env=os.environ.copy(); env["NETSIM_AUTO"]="1"
                process=subprocess.Popen(cmd,cwd=c.binary,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env)
                PROCESSES[c.id]=process
                lines=[]; deadline=time.monotonic()+7200
                for line in process.stdout:
                    lines.append(line.rstrip()); c.log.append(f"{stamp()} Run {index} | {line.rstrip()}")
                    if time.monotonic()>deadline: process.kill(); raise RuntimeError("NetSimCore timed out after two hours")
                code=process.wait(); proc_text="\n".join(lines); (run_dir/"process.log").write_text(proc_text,encoding="utf-8")
                PROCESSES.pop(c.id,None)
                if c.cancel: record["status"]="Cancelled"; record["error"]="Campaign cancelled by user"; raise RuntimeError("__cancelled__")
                if code: raise RuntimeError(f"NetSimCore exited with code {code}")
            metrics_path=run_dir/"Metrics.xml"
            if not metrics_path.is_file(): record["status"]="Metrics missing"; record["error"]="NetSim completed but did not produce Metrics.xml"
            else:
                for spec in c.metric_specs:
                    found, metric_error=resolve_metric(metrics_path,spec)
                    record["metrics"][spec["label"]]=found["value"] if found else ""
                    if not found: record["status"]="Metrics missing"; record["error"]+=f" {metric_error}"
                if record["status"]=="Running": record["status"]="Passed"
            c.log.append(f"{stamp()} Run {index}: {record['status']}. {proc_text[:250]}")
        except Exception as exc:
            if str(exc)=="__cancelled__": c.log.append(f"{stamp()} Run {index} cancelled")
            else: record["status"]="Failed"; record["error"]=str(exc); c.log.append(f"{stamp()} Run {index} failed: {exc}")
        record["end"]=stamp(); record["duration"]=round((datetime.fromisoformat(record['end'])-datetime.fromisoformat(started)).total_seconds(),3)
        write_results(c,campaign_dir); write_campaign(c)
    PROCESSES.pop(c.id,None); c.state="Cancelled" if c.cancel else "Complete"; c.ended=stamp(); write_results(c,campaign_dir); write_campaign(c); c.log.append(f"{stamp()} Campaign {c.state.lower()}")

def write_results(c, folder):
    fields=["campaign_id","run","status"]+[p['label'] for p in c.parameters]+[m['label'] for m in c.metric_specs]+["start","end","duration","output_folder","error"]
    with (folder/"cumulative_results.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in c.runs:
            row={"campaign_id":c.id,"run":r.get("run"),"status":r.get("status"),**r.get("inputs",{}),**r.get("metrics",{}),"start":r.get("start",""),"end":r.get("end",""),"duration":r.get("duration",""),"output_folder":r.get("output_folder",""),"error":r.get("error","")}; w.writerow(row)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT),**kw)
    def json(self,data,status=200):
        raw=json.dumps(data).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/campaigns":
            load_persisted(); return self.json([summary(c) for c in CAMPAIGNS.values()])
        if path.startswith("/api/campaign/"):
            load_persisted(); c=CAMPAIGNS.get(path.rsplit("/",1)[-1]); return self.json({**asdict(c),"summary":summary(c)} if c else {"error":"Unknown campaign"},200 if c else 404)
        if path.startswith("/download/"):
            parts=path.split("/"); c=CAMPAIGNS.get(parts[2]) if len(parts)>2 else None
            file=Path(c.output)/c.id/"cumulative_results.csv" if c else None
            if file and file.exists():
                self.send_response(200); self.send_header("Content-Type","text/csv"); self.send_header("Content-Disposition",f'attachment; filename="{c.id}.csv"'); self.end_headers(); self.wfile.write(file.read_bytes()); return
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length",0)); body=self.rfile.read(length)
        if path=="/api/upload":
            name=Path(self.headers.get("X-Filename","upload.xml")).name
            if Path(name).suffix.lower() not in (".xml", ".netsim"): return self.json({"error":"Only XML or .netsim files can be uploaded"},400)
            target=UPLOADS/(uuid.uuid4().hex+"_"+name); target.write_bytes(body)
            return self.json({"path":str(target)})
        if path=="/api/browse":
            try:
                import tkinter as tk
                from tkinter import filedialog
                root=tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
                kind=self.headers.get("X-Kind", "file")
                if kind=="folder": selected=filedialog.askdirectory(parent=root)
                elif kind=="license": selected=filedialog.askopenfilename(parent=root,title="Select license file")
                else: selected=filedialog.askopenfilename(parent=root,title="Select file")
                root.destroy(); return self.json({"path":selected})
            except Exception as exc: return self.json({"error":f"Windows file picker unavailable: {exc}"},500)
        try: data=json.loads(body or b"{}")
        except json.JSONDecodeError: return self.json({"error":"Invalid JSON"},400)
        try:
            if path=="/api/parse-config":
                file=Path(data["path"]); err=xml_error(file)
                return self.json({"error":err},400) if err else self.json({"parameters":element_catalog(file)})
            if path=="/api/parse-metrics":
                file=Path(data["path"]); err=xml_error(file)
                return self.json({"error":err},400) if err else self.json({"metrics":metric_catalog(file)})
            if path=="/api/start":
                c=Campaign(id="campaign_"+datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+uuid.uuid4().hex[:4], **data)
                validate(c); CAMPAIGNS[c.id]=c; threading.Thread(target=run_campaign,args=(c,),daemon=True).start(); return self.json({"id":c.id})
            if path.startswith("/api/cancel/"):
                c=CAMPAIGNS.get(path.rsplit("/",1)[-1]); c.cancel=True
                process=PROCESSES.get(c.id)
                if process and process.poll() is None: process.kill(); c.log.append(f"{stamp()} Cancellation requested; NetSimCore process terminated.")
                return self.json({"ok":True})
        except Exception as exc: return self.json({"error":str(exc)},400)
        return self.json({"error":"Not found"},404)

def summary(c):
    states={s:0 for s in ["Pending","Running","Passed","Failed","Metrics missing","Cancelled"]}
    for r in c.runs: states[r.get("status","Pending")]=states.get(r.get("status","Pending"),0)+1
    total=1
    try:
        for p in c.parameters: total*=len(values_for(p))
    except: total=0
    return {"id":c.id,"name":c.name,"state":c.state,"started":getattr(c,'started',''),"ended":getattr(c,'ended',''),"total":total,"finished":len([r for r in c.runs if r.get('status') not in ('Running','Pending')]),"states":states,"runs":c.runs,"log":c.log}

def validate(c):
    if not c.parameters: raise ValueError("Select at least one input parameter")
    ids=[p.get("nodeId") for p in c.parameters]
    if len(ids)!=len(set(ids)): raise ValueError("A configuration item can be swept only once")
    count=1
    for p in c.parameters: count*=len(values_for(p))
    if count>int(c.max_runs): raise ValueError(f"{count} combinations exceed the organizer limit of {c.max_runs}")
    if not Path(c.config).is_file(): raise ValueError("Configuration.netsim path does not exist")
    if xml_error(c.config): raise ValueError("Configuration.netsim is malformed XML")
    if c.metrics and (not Path(c.metrics).is_file() or xml_error(c.metrics)): raise ValueError("Metrics.xml is missing or malformed")
    if c.metric_specs:
        for spec in c.metric_specs:
            spec["referencePath"]=c.metrics
            found, err=resolve_metric(c.metrics,spec)
            if not found: raise ValueError(err)
    if not c.mock and not Path(c.binary,"NetSimCore.exe").is_file(): raise ValueError("Select a binary folder containing NetSimCore.exe")
    if c.license_type=="file" and not Path(c.license_value).is_file(): raise ValueError("Select a valid license file")

def load_persisted():
    if not INDEX.exists(): return
    try: known=json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError): return
    for cid, folder in known.items():
        file=Path(folder)/"campaign.json"
        if cid not in CAMPAIGNS and file.exists():
            try: CAMPAIGNS[cid]=Campaign(**json.loads(file.read_text(encoding="utf-8")))
            except (TypeError,json.JSONDecodeError): pass

if __name__=="__main__":
    print("NetSim Sweeper running at http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1",8765),Handler).serve_forever()
