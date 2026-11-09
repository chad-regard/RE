"""Where the FUCK is my stock — furniture movement and overlap web app."""
from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from stock_engine import (
    parse_document, read_stock_csv, build_bookings,
    assess_movement, build_furniture_outputs, job_identity,
    DEFAULT_TRACKED_TERMS, DEFAULT_EXCLUDED_TERMS,
)

st.set_page_config(page_title="Where the FUCK is my stock", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""
<style>
  [data-testid="stAppViewContainer"] {background: #fbfaf7; color:#141414;}
  [data-testid="stHeader"] {background: transparent;}
  [data-testid="stSidebar"] {background: #f4f1eb;}
  .block-container {max-width: 1200px; padding-top: 2.5rem; padding-bottom: 5rem;}
  .hero {min-height: 35vh; display: flex; flex-direction: column; align-items: center; justify-content:center; text-align:center;}
  .hero h1 {font-family:Arial,Helvetica,sans-serif; font-size:clamp(2.5rem,6vw,6rem); letter-spacing:-.065em; line-height:1.03; font-weight:900; max-width:950px; margin:0; color:#151515;}
  .hero p {font-size:1rem; color:#777; margin-top:1.5rem;}
  div[data-testid="stFileUploader"] {border: 1px dashed #a7a39a; border-radius:18px; padding:12px; background:#fff;}
  .muted {color:#777; font-size:.86rem;}
  .section-label {font-size: .74rem; text-transform:uppercase; letter-spacing:.15em; font-weight:800; color:#777; margin-top:22px;}
  [data-testid="stMetric"] {background:#fff;border:1px solid #ece7de;border-radius:14px;padding:15px;}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="hero"><h1>Where the FUCK is my stock</h1><p>Upload event quotations. Find the furniture clashes. Know exactly what to count.</p></div>', unsafe_allow_html=True)

left, right = st.columns([2, 1], gap="large")
with left:
    uploaded = st.file_uploader("Upload quotations (PDF or ZIP)", type=["pdf", "zip"], accept_multiple_files=True, help="Upload multiple event quotations at once. ZIP files may contain PDFs in subfolders.")
with right:
    inventory_upload = st.file_uploader("Optional: upload stock counts (CSV)", type=["csv"], help="CSV columns: supplier,item_name,available_qty. Without it, demand is shown but shortages are marked unknown.")
    turnaround_hours = st.number_input("Hours needed after collection before reuse", min_value=0.0, max_value=240.0, value=4.0, step=1.0)

with st.expander("Furniture matching and date settings"):
    st.caption("The app ignores pricing, discounts, table linens, florals and service charges. Matching is supplier-specific and uses item names. Adjust terms for your inventory.")
    tracked_text = st.text_area("Furniture keywords (comma separated)", value=", ".join(DEFAULT_TRACKED_TERMS), height=85)
    excluded_text = st.text_area("Exclude descriptions containing (comma separated)", value=", ".join(DEFAULT_EXCLUDED_TERMS), height=85)
    enable_ocr = st.checkbox("Try OCR for scanned PDFs (requires Tesseract installed on server)", value=False)

st.caption("Only quotations you upload in this session are analysed. No data is sent to an AI service. Dates or quantities that cannot be verified are flagged for review.")

def gather_pdfs(files, folder):
    output=[];warnings=[];digest=set()
    for uploaded_file in files:
        payload=uploaded_file.getvalue()
        candidates=[]
        if uploaded_file.name.lower().endswith('.pdf'):
            candidates=[(Path(uploaded_file.name).name,payload)]
        elif uploaded_file.name.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as z:
                    for entry in z.infolist():
                        if entry.is_dir() or not entry.filename.lower().endswith('.pdf'):continue
                        if entry.file_size>35*1024*1024:
                            warnings.append(f"Skipped oversized PDF: {entry.filename}")
                            continue
                        candidates.append((Path(entry.filename).name,z.read(entry)))
            except (zipfile.BadZipFile,ValueError) as exc:
                warnings.append(f"Unreadable ZIP {uploaded_file.name}: {exc}")
        for name,data in candidates:
            if len(data)>35*1024*1024:
                warnings.append(f"Skipped oversized PDF: {name}");continue
            fingerprint=hashlib.sha256(data).hexdigest()
            if fingerprint in digest:continue
            digest.add(fingerprint)
            clean="".join(c for c in name if c.isalnum() or c in " -_.()") or "quotation.pdf"
            destination=folder/f"{len(output)+1:03d}_{clean}"
            destination.write_bytes(data)
            output.append(destination)
    return output,warnings

@st.cache_data(show_spinner=False, max_entries=6)
def analyze(uploaded_bytes, inventory_bytes, buffer_hours, tracked, excluded, ocr):
    with tempfile.TemporaryDirectory(prefix="furniture_web_") as tmp:
        root=Path(tmp)
        class Uploaded:
            def __init__(self,name,payload): self.name=name;self.payload=payload
            def getvalue(self):return self.payload
        files=[Uploaded(name,data) for name,data in uploaded_bytes]
        paths,warnings=gather_pdfs(files,root)
        if not paths:
            raise ValueError("No readable PDF files were provided.")
        if len(paths)>250:
            raise ValueError("Upload at most 250 different PDFs in one run.")
        events=[]
        reviews=[]
        for path in paths:
            try:
                ev=parse_document(path,enable_ocr=ocr)
                # The engine stores the temp input basename for source attribution.
                events.append(ev)
            except Exception as exc:
                reviews.append({'level':'URGENT','job':path.name,'details':f'Could not extract PDF: {exc}'})
        if not events:
            raise ValueError("No PDF could be read. Scanned documents may require OCR.")
        stock={}
        if inventory_bytes is not None:
            stock,stock_warnings=read_stock_csv(inventory_bytes)
            reviews.extend({'level':'CHECK STOCK','job':'Inventory','details':m} for m in stock_warnings)
        bookings,extraction_reviews,effective=build_bookings(
            events,tracked_terms=tracked,excluded_terms=excluded,aliases={})
        reviews.extend(extraction_reviews)
        reviews.extend({'level':'CHECK FILE','job':'Upload','details':x} for x in warnings)
        clashes,needs,movements=assess_movement(bookings,stock=stock,turnaround_hours=buffer_hours)
        report_dir=root/'reports'
        results=build_furniture_outputs(effective,bookings,clashes,needs,movements,reviews,str(report_dir),turnaround_hours=buffer_hours)
        artifacts={key:Path(path).read_bytes() for key,path in results.items() if key in ('pdf','zip','template')}
        return {
            'events':len(set(job_identity(e) for e in effective)),
            'pdfs':len(effective),
            'allocations':len(bookings),
            'clashes':clashes,'needs':needs,'movements':movements,'reviews':reviews,
            'shortage_types':sum(r['Availability']=='SHORTAGE' for r in needs),
            'unknown_types':sum(r['Availability']=='STOCK UNKNOWN - CONFIRM' for r in needs),
            'extra_units':sum(r['Extra Units Required'] or 0 for r in needs if r['Availability']=='SHORTAGE'),
            'files':artifacts,
        }

if not uploaded:
    st.markdown('<p class="section-label">1 / Drop your PDF files above to begin</p>',unsafe_allow_html=True)
    st.stop()

if st.button("Analyse furniture bookings", type="primary", use_container_width=True):
    names_and_bytes=tuple((f.name,f.getvalue()) for f in uploaded)
    inventory_bytes=inventory_upload.getvalue() if inventory_upload else None
    tracked=tuple(x.strip() for x in tracked_text.split(',') if x.strip())
    excluded=tuple(x.strip() for x in excluded_text.split(',') if x.strip())
    try:
        with st.spinner("Extracting jobs, calculating overlapping stock requirements and preparing reports…"):
            st.session_state.analysis=analyze(names_and_bytes,inventory_bytes,turnaround_hours,tracked,excluded,enable_ocr)
            st.session_state.analysis_signature=hashlib.sha256(repr((names_and_bytes,inventory_bytes,turnaround_hours,tracked,excluded,enable_ocr)).encode()).hexdigest()
    except Exception as exc:
        st.error(f"Could not complete analysis: {exc}")
        st.stop()

result=st.session_state.get('analysis')
if result is None:
    st.info("Choose your PDFs and click **Analyse furniture bookings**.")
    st.stop()

st.divider()
st.subheader("Your furniture stock report")
metrics=st.columns(5)
for col,(label,value) in zip(metrics,[
    ('Jobs reviewed',result['events']),('Furniture clashes',len(result['clashes'])),
    ('Stock shortages',result['shortage_types']),('Extra units needed',result['extra_units']),
    ('Unknown stock counts',result['unknown_types'])]):
    col.metric(label,value)
if result['unknown_types']:
    st.warning(f"{result['unknown_types']} furniture item(s) have no stock count. The app can show peak demand but cannot confirm how many extra pieces to source until you upload inventory counts.")
if result['reviews']:
    st.info(f"{len(result['reviews'])} booking/date/detail check(s) need review. See **Details to confirm** below.")

tabs=st.tabs(['Furniture booking clashes','Furniture stock needed','Delivery & collection calendar','Details to confirm'])
with tabs[0]:
    st.markdown("**Both jobs, event dates, delivery and collection windows, quantities and any known shortage.**")
    rows=result['clashes']
    if rows:
        cols=['Furniture Item','Job 1','Job 1 Event Date','Job 1 Delivery Date & Time','Job 1 Collection Date & Time','Job 1 Quantity',
              'Job 2','Job 2 Event Date','Job 2 Delivery Date & Time','Job 2 Collection Date & Time','Job 2 Quantity',
              'Total Units Required','Stock Available','Extra Units Required','Availability','Time Confirmation Needed']
        st.dataframe(pd.DataFrame(rows)[cols].fillna('Unknown'),hide_index=True,use_container_width=True)
    else:st.success("No overlapping furniture allocations found between different jobs in the uploaded quotations.")
with tabs[1]:
    st.markdown("**Peak simultaneous demand across all jobs**, including three or more overlapping events.")
    if result['needs']:
        st.dataframe(pd.DataFrame(result['needs']).fillna('Unknown'),hide_index=True,use_container_width=True)
    else:st.info("No furniture lines were recognised. Adjust the furniture keywords or inspect the quotation format.")
with tabs[2]:
    if result['movements']:
        st.dataframe(pd.DataFrame(result['movements']).fillna('Unknown'),hide_index=True,use_container_width=True)
    else:st.info("No delivery / collection records extracted.")
with tabs[3]:
    if result['reviews']:
        st.dataframe(pd.DataFrame(result['reviews']),hide_index=True,use_container_width=True)
    else:st.success("No booking details flagged for review.")

st.subheader("Download the counting report")
c1,c2,c3=st.columns(3)
with c1:st.download_button("Download PDF report",result['files']['pdf'],"Furniture_Booking_Clash_Report.pdf","application/pdf",use_container_width=True)
with c2:st.download_button("Download all reports (ZIP)",result['files']['zip'],"Furniture_Booking_Reports.zip","application/zip",use_container_width=True)
with c3:st.download_button("Download stock-count template",result['files']['template'],"Furniture_Stock_Count_Template.csv","text/csv",use_container_width=True)
st.caption("Stock needed is based on the highest simultaneous bookings, not the sum of all dates. Unconfirmed quotations, estimated times and midnight collections must be checked before dispatch.")
