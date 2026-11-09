"""Furniture movement and concurrent-stock requirements from event PDFs.
No financial or discount checks are performed.
"""
import csv
import hashlib
import io
import os
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz

DATE_PATTERN = r'(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})'
MONEY = r'(?:R|ZAR\s*)?\s*[\d,]+(?:\.\d{2})?'
# First-column description; then six aligned numerical columns, as in the sample quotation.
LINE_RE = re.compile(r'^\s*(?P<desc>.*?)\s{2,}(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>R\s*[\d,.]+)\s+(?P<disc>\d+(?:\.\d+)?%)\s+(?P<vat>\d+(?:\.\d+)?%)\s+(?P<excl>R\s*[\d,.]+)\s+(?P<incl>R\s*[\d,.]+)\s*$', re.I)
SERVICE_WORDS = ('delivery', 'collection', 'labour', 'labor', 'transport', 'setup', 'set up', 'electrician', 'florist fee', 'after hour', 'crew', 'staffing', 'installation', 'cleaning', 'deposit', 'security', 'discount', 'hire fee')
CUSTOM_WORDS = ('floral', 'flower arrangement', 'fruit arrangement', 'arrangement for', 'vinyl full wrap', 'printing', 'custom graphic', 'styling', 'centre pieces to theme')
ISSUE_PRIORITY = {'HIGH':0,'MEDIUM':1,'LOW':2,'INFO':3}


def norm(x):
    return re.sub(r'\s+', ' ', str(x or '')).strip()


def norm_key(x):
    return re.sub(r'[^a-z0-9]+', ' ', norm(x).lower()).strip()


def money(x):
    if x is None: return None
    try: return Decimal(re.sub(r'[^\d.\-]', '', str(x)))
    except InvalidOperation: return None


def amount_string(x):
    return 'R{:,.2f}'.format(float(x)) if x is not None else 'Not found'


def parse_date(x):
    if not x: return None
    x = norm(x).replace(',', '')
    for fmt in ('%d/%m/%Y','%d-%m-%Y','%d/%m/%y','%d-%m-%y','%d %B %Y','%d %b %Y','%Y-%m-%d'):
        try: return datetime.strptime(x,fmt).date()
        except ValueError: pass
    return None


def iso_date(x):
    return x.isoformat() if x else ''


def parse_clock(x):
    if not x: return None
    s=norm(x).upper().replace('.', '')
    if 'MIDNIGHT' in s: return (0,0,True)
    if 'NOON' in s: return (12,0,False)
    m=re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b',s)
    if m:
        h=int(m.group(1)) % 12
        if m.group(3)=='PM': h+=12
        return (h,int(m.group(2) or 0),False)
    m=re.search(r'\b(\d{1,2}):(\d{2})\b',s)
    if m and int(m.group(1))<24 and int(m.group(2))<60:
        return (int(m.group(1)),int(m.group(2)),False)
    return None


def text_from_page(page, enable_ocr=False):
    txt=page.get_text('text', sort=True)
    if len(txt.strip())>=80 or not enable_ocr:
        return txt,False
    # OCR is optional and runs entirely in the Colab VM (Tesseract must be installed).
    import pytesseract
    from PIL import Image
    px=page.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False)
    img=Image.open(io.BytesIO(px.tobytes('png')))
    return pytesseract.image_to_string(img,config='--psm 3'),True


def get_field(text, label, capture=DATE_PATTERN):
    m=re.search(r'(?im)^\s*'+label+r'\s*:\s*('+capture+r')',text)
    return m.group(1).strip() if m else None


def get_label_date(text,label):
    return parse_date(get_field(text,label))


def extract_ref(text):
    lines=text.splitlines()
    for idx,line in enumerate(lines):
        if re.search(r'\bREFERENCE\s*:',line,re.I):
            value=line.split(':',1)[1].strip()
            for l in lines[idx+1:idx+5]:
                if re.match(r'\s*(?:DATE|DUE DATE|SALES REP|PAGE|OVERALL DISCOUNT|FROM|TO)\s*:',l,re.I): break
                if re.match(r'\s*(?:DATE|DUE DATE|FROM|TO)\b',l,re.I): break
                if l.strip():value+=' '+l.strip()
            return norm(value)
    return ''


def extract_logistics(text, name):
    # Date/time are deliberately kept separate so a midnight ambiguity can be reported.
    m=re.search(r'(?im)\b'+name+r'\s*[-:]\s*('+DATE_PATTERN+r')([^\n]{0,30})',text)
    if not m:return (None,None,False)
    d=parse_date(m.group(1))
    clock=parse_clock(m.group(2))
    if not clock:
        lineend=text.find('\n',m.end())
        follow=text[m.end():lineend+1] if lineend>=0 else ''
        following=text[lineend+1:].splitlines()[:2] if lineend>=0 else []
        for s in [follow]+following:
            clock=parse_clock(s)
            if clock:break
            if re.search(r'\b(?:delivery|collection)\s*[-:]|Description|VAT NO',s,re.I):break
    return (d, '{:02d}:{:02d}'.format(clock[0],clock[1]) if clock else None, bool(clock and clock[2]))


def extract_party_and_venue(page):
    """Preferred layout extraction for the example; blank on unfamiliar layouts."""
    w,h=page.rect.width,page.rect.height
    client=''; vendor=''; venue=''
    words=page.get_text('words')
    froms=[t for t in words if t[4].upper()=='FROM' and t[1]<h*.4]
    tos=[t for t in words if t[4].upper()=='TO' and t[1]<h*.4]
    if froms and tos:
        y=min(froms[0][1],tos[0][1])
        vt=page.get_text('text',clip=fitz.Rect(12,y+9,w*.50,y+43),sort=True)
        ct=page.get_text('text',clip=fitz.Rect(w*.50,y+9,w*.78,y+43),sort=True)
        vendor=norm(vt)
        client=norm(ct)
    headings=[q for q in words if q[4].upper().startswith('PHYSICAL') and q[0]>w*.6 and q[1]<h*.45]
    if headings:
        hy=headings[0][1]
        vl=page.get_text('text',clip=fitz.Rect(w*.72,hy+10,w-6,hy+36),sort=True).splitlines()
        venue=norm(' '.join(vl))
    return vendor,client,venue


def parse_table(pages):
    items=[]; section='General'; table_seen=False
    for pno,txt in enumerate(pages,1):
        inside=False
        last_item=None
        for line in txt.splitlines():
            if re.search(r'^\s*Description\s{2,}Quantity\b',line,re.I):
                inside=True;table_seen=True;continue
            if not inside:continue
            if re.search(r'^\s*[-–]\s*Please note\b|^\s*Total Discount\s*:',line,re.I):break
            s=line.strip()
            if not s:continue
            m=LINE_RE.match(line)
            if m:
                desc0=m.group('desc').strip()
                cm=re.match(r'^([^\s]+)\s+-\s+(.+)$',desc0)
                if cm:code,desc=cm.group(1).strip(),cm.group(2).strip()
                else:code,desc='',desc0
                qty=money(m.group('qty'));unit=money(m.group('unit'))
                disc=money(m.group('disc'));vat=money(m.group('vat'))
                excl=money(m.group('excl'));incl=money(m.group('incl'))
                if (code.upper() in {'H001','HEADER','SECTION'} and unit == 0 and excl == 0) or (unit == 0 and excl == 0 and desc.isupper()):
                    section=desc;last_item=None;continue
                item={'page':pno,'section':section,'code':code if code not in ('_','-') else '',
                      'description':desc,'qty':qty,'unit_price':unit,'discount_pct':disc,
                      'vat_pct':vat,'excl':excl,'incl':incl}
                items.append(item);last_item=item
            elif last_item and not re.search(r'\b(?:Total|Grand Total|VAT|Office hours|ACCOUNT|Due Date|Quote)\b',s,re.I):
                # Continuation of the preceding printed item description.
                if len(s)<95 and not s.startswith('- '):last_item['description'] += ' '+s
    for item in items:
        desc=norm_key(item['description'])
        if any(w in desc for w in SERVICE_WORDS):item['kind']='Service / fee'
        elif any(w in desc for w in CUSTOM_WORDS):item['kind']='Custom / consumable'
        else:item['kind']='Rental / physical item'
    return items,table_seen


def parse_document(filepath, enable_ocr=False):
    path=Path(filepath)
    with open(path,'rb') as f: digest=hashlib.sha256(f.read()).hexdigest()
    doc=fitz.open(path)
    texts=[]; ocr_pages=[]
    for pno,p in enumerate(doc,1):
        txt,was_ocr=text_from_page(p,enable_ocr)
        texts.append(txt)
        if was_ocr:ocr_pages.append(pno)
    full='\n'.join(texts);first=texts[0] if texts else ''
    reference=extract_ref(first)
    quote_num=get_field(first,'NUMBER',r'[A-Za-z0-9-]+')
    if not quote_num:
        quote_num=get_field(first,r'(?:QUOTE|QUOTATION|INVOICE)\s*(?:NUMBER|NO)',r'[A-Za-z0-9-]+')
    doc_type='Quote' if re.search(r'\bQUOTE\b|\bQUOTATION\b',first[:300],re.I) else ('Invoice' if re.search(r'\bINVOICE\b',first[:300],re.I) else 'Other PDF')
    issued=get_label_date(first,'DATE');due=get_label_date(first,'DUE DATE')
    event_label_date_match=re.search(DATE_PATTERN,reference,re.I)
    event_date=parse_date(event_label_date_match.group(0)) if event_label_date_match else None
    for l in ('EVENT DATE','FUNCTION DATE','WEDDING DATE','EVENT ON'):
        d=get_label_date(first,l)
        if d: event_date=d; break
    delivery_d,delivery_t,delivery_mid=extract_logistics(full,'Delivery')
    collection_d,collection_t,collection_mid=extract_logistics(full,'Collection')
    if not event_date:event_date=delivery_d
    vendor,client,venue=extract_party_and_venue(doc[0]) if doc.page_count else ('','','')
    if not client:
        m=re.search(r'(?im)^\s*CLIENT\s*:\s*(.+)$|^\s*CUSTOMER\s*:\s*(.+)$',first)
        if m: client=norm(m.group(1) or m.group(2))
    if not venue:
        m=re.search(r'(?im)^\s*(?:VENUE|LOCATION|EVENT LOCATION)\s*:\s*(.+)$',first)
        if m: venue=norm(m.group(1))
    if not vendor:
        m=re.search(r'(?im)^\s*(?:SUPPLIER|FROM)\s*:\s*(.+)$',first)
        if m:vendor=norm(m.group(1))
    full_total=re.search(r'(?im)\bGrand\s+Total\s*:\s*(R\s*[\d,.]+)',full)
    total=money(full_total.group(1)) if full_total else None
    if total is None:
        mt=re.search(r'(?im)^\s*Total(?:\s+Inclusive)?\s*:\s*(R\s*[\d,.]+)',full)
        if mt:total=money(mt.group(1))
    discount_m=re.search(r'(?im)\bTotal\s+Discount\s*:\s*(R\s*[\d,.]+)',full)
    discount_total=money(discount_m.group(1)) if discount_m else None
    items,table_seen=parse_table(texts)
    label=reference
    if event_label_date_match:
        label=norm(reference[:event_label_date_match.start()]+reference[event_label_date_match.end():])
    label=label or client or path.stem
    # Derive all-day effective period if event metadata insufficient; never imply a verified end time.
    start=datetime.combine(delivery_d or event_date,datetime.min.time()) if (delivery_d or event_date) else None
    if start and delivery_t:
        h,minute=map(int,delivery_t.split(':'));start=start.replace(hour=h,minute=minute)
    end=None; time_assumed=False
    if collection_d and collection_t:
        h,minute=map(int,collection_t.split(':'))
        end=datetime.combine(collection_d,datetime.min.time()).replace(hour=h,minute=minute)
    if start and (end is None or end<=start):
        # Conservative same-day event occupancy window; issue explains this fallback.
        end=datetime.combine((event_date or delivery_d or start.date())+timedelta(days=1),datetime.min.time())
        if end<=start:end=start+timedelta(days=1)
        time_assumed=True
    ev={'file':path.name,'sha256':digest,'pages':len(texts),'ocr_pages':ocr_pages,'document_type':doc_type,
        'document_no':quote_num or '', 'reference':reference,'title':label,'customer':client,'supplier':vendor,'venue':venue,
        'event_date':iso_date(event_date),'issued_date':iso_date(issued),'due_date':iso_date(due),
        'delivery_date':iso_date(delivery_d),'delivery_time':delivery_t or '',
        'collection_date':iso_date(collection_d),'collection_time':collection_t or '',
        'collection_midnight':collection_mid,'delivery_midnight':delivery_mid,
        'window_start':start.isoformat(timespec='minutes') if start else '',
        'window_end':end.isoformat(timespec='minutes') if end else '',
        'window_assumed':time_assumed,'grand_total':total,'discount_total':discount_total,
        'items':items,'table_seen':table_seen,'text_len':len(full.strip())}
    return ev


def event_key(ev):
    """Do not merge by customer/date alone: one client can have two separate events."""
    if ev.get('event_date') and ev.get('reference'):
        return ev['event_date']+'|'+norm_key(ev['reference'])
    return 'doc|'+ev['sha256']


def issue(severity,category,description,event_a='',event_b='',item='',source='',recommended=''):
    return {'severity':severity,'category':category,'details':description,'event_a':event_a,
            'event_b':event_b,'item':item,'source':source,'action':recommended}



# Keep the tracking list editable. This deliberately excludes linen, tableware,
# flowers, electrical/labour services and small decoration items.
DEFAULT_TRACKED_TERMS = (
    'chair','table','sofa','couch','settee','bench','stool','ottoman','lounge',
    'bar','plinth','podium','console','cabinet','shelf','desk','door',
    'mirror','umbrella','dancefloor','dance floor','light stand',
    'chandelier','lamp','partition','screen','stage','lectern'
)
DEFAULT_EXCLUDED_TERMS = (
    'tablecloth','table cloth','table runner','table number','table setting',
    'tableware','napkin','cutlery','crockery','glassware','glass plate',
    'draping','floral','flower','fruit arrangement','arrangement',
    'centre piece','center piece','vase','vessel','statuette','candle',
    'vinyl','printing','wrap','florist','delivery','collection',
    'setup','labour','after hour','electrician','transport','fee'
)

def has_term(description, phrase):
    return bool(re.search(r'(?<![a-z0-9])'+re.escape(phrase).replace(r'\ ',r'\s+')+r'(?![a-z0-9])', norm_key(description)))


def is_furniture(item, include=DEFAULT_TRACKED_TERMS, exclude=DEFAULT_EXCLUDED_TERMS):
    if item.get('kind') != 'Rental / physical item': return False
    desc = standard_name(item.get('description'))
    if any(has_term(desc,t) for t in exclude):return False
    return any(has_term(desc,t) for t in include)


def standard_name(name, aliases=None):
    key=norm_key(name)
    # Drop unambiguous spelling variants without losing sizes, styles or colours.
    key=re.sub(r'\b(tables|chairs|benches|mirrors|doors|stools|bars|umbrellas)\b',
               lambda m: {'tables':'table','chairs':'chair','benches':'bench','mirrors':'mirror',
                          'doors':'door','stools':'stool','bars':'bar','umbrellas':'umbrella'}[m.group()],key)
    normalized_aliases={norm_key(k):norm_key(v) for k,v in (aliases or {}).items()}
    return normalized_aliases.get(key,key)


def job_identity(ev):
    label=norm_key(ev.get('reference') or ev.get('title'))
    return ((ev.get('event_date') or ev.get('delivery_date') or 'unknown')+'|'+label) if label else 'pdf|'+ev['sha256']


def doc_priority(ev):
    return (ev.get('issued_date') or '',ev.get('file') or '')


def effective_documents(events):
    """Deduplicate identical PDF uploads and replace same-number revisions.
    Distinct document numbers for one event are treated as additive add-ons.
    """
    reviews=[];unique_hash=set();files=[]
    for ev in events:
        if ev['sha256'] in unique_hash:
            reviews.append({'level':'REVIEW','job':ev['title'],'details':f'Identical document uploaded more than once: {ev["file"]}. Counted once.'})
            continue
        unique_hash.add(ev['sha256']); files.append(ev)
    selected={};no_number=[]
    for ev in files:
        if not ev.get('document_no'):
            no_number.append(ev);continue
        k=(norm_key(ev.get('supplier')),norm_key(ev['document_no']))
        if k not in selected:
            selected[k]=ev
        else:
            prev=selected[k]
            keep=max((prev,ev),key=doc_priority)
            selected[k]=keep
            reviews.append({'level':'REVIEW','job':keep['title'],
                'details':f'Different versions of {ev["document_no"]}: retained {keep["file"]}; verify that this is the latest approved version.'})
    return list(selected.values())+no_number,reviews


def inventory_key(supplier,name):
    return 'supplier:'+norm_key(supplier)+'|name:'+standard_name(name)


def read_stock_csv(content,aliases=None):
    """Expected supplier,item_code,item_name,available_qty.
    Names are preferred to generic codes because some suppliers reuse a code for
    small/medium/large variants (e.g. CRE009 in the sample).
    """
    if isinstance(content,bytes):content=content.decode('utf-8-sig')
    result={}; warnings=[]
    for n,row in enumerate(csv.DictReader(io.StringIO(content)),2):
        row={norm_key(k).replace(' ','_'):norm(v) for k,v in row.items() if k}
        name=row.get('item_name') or row.get('description')
        supplier=row.get('supplier','')
        value=money(row.get('available_qty',''))
        if not name or value is None or value<0:
            warnings.append(f'Stock CSV row {n} skipped: supply item_name and a non-negative available_qty.')
            continue
        key=inventory_key(supplier,standard_name(name,aliases))
        if key in result:warnings.append(f'Stock CSV repeats {name} / {supplier}: last quantity used.')
        result[key]=value
    return result,warnings


def stock_for(stock,supplier,canonical_name):
    return stock.get(inventory_key(supplier,canonical_name),stock.get(inventory_key('',canonical_name)))


def build_bookings(events,tracked_terms=DEFAULT_TRACKED_TERMS,excluded_terms=DEFAULT_EXCLUDED_TERMS,aliases=None):
    docs,reviews=effective_documents(events)
    # A job can include several additive quotations with different document numbers.
    records={}
    per_job_docs=defaultdict(list)
    for ev in docs:
        job=job_identity(ev)
        per_job_docs[job].append(ev)
        if not ev['window_start'] or not ev['window_end']:
            reviews.append({'level':'URGENT','job':ev['title'],
                            'details':f'Cannot establish a usable movement window in {ev["file"]}; furniture excluded from overlap calculations until corrected.'})
            continue
        if ev.get('collection_midnight'):
            reviews.append({'level':'CHECK TIME','job':ev['title'],
                            'details':f'{ev["file"]}: collection MIDNIGHT interpreted as end of event day (next date 00:00). Confirm actual collection date/time.'})
        elif ev.get('window_assumed'):
            reviews.append({'level':'CHECK TIME','job':ev['title'],
                            'details':f'{ev["file"]}: no reliable collection time; assumed to end at midnight after the event.'})
        for it in ev['items']:
            if not is_furniture(it,tracked_terms,excluded_terms):continue
            qty=it.get('qty')
            if qty is None or qty<=0:continue
            canonical=standard_name(it['description'],aliases)
            supplier=norm_key(ev.get('supplier')) or ('unknown supplier / '+ev['file'])
            key=(supplier,canonical,job)
            if key not in records:
                records[key]={'supplier':ev.get('supplier') or 'Unknown supplier','key':canonical,
                    'item':it['description'],'job_id':job,'job':ev['title'],
                    'event_date':ev['event_date'] or ev['delivery_date'],
                    'venue':ev.get('venue') or '', 'qty':Decimal(0),
                    'start':datetime.fromisoformat(ev['window_start']),
                    'end':datetime.fromisoformat(ev['window_end']),
                    'time_assumed':bool(ev['window_assumed'] or ev['collection_midnight']),
                    'source_pages':[],'source_files':set(),'document_nos':set()}
            rec=records[key]
            rec['qty']+=qty
            rec['start']=min(rec['start'],datetime.fromisoformat(ev['window_start']))
            rec['end']=max(rec['end'],datetime.fromisoformat(ev['window_end']))
            rec['time_assumed'] |= bool(ev['window_assumed'] or ev['collection_midnight'])
            rec['source_pages'].append(ev['file']+' p.'+str(it['page']))
            rec['source_files'].add(ev['file'])
            if ev['document_no']:rec['document_nos'].add(ev['document_no'])
    for job,job_docs in per_job_docs.items():
        if len(job_docs)>1:
            docs_desc=', '.join(sorted(e['file'] for e in job_docs))
            reviews.append({'level':'REVIEW','job':job_docs[0]['title'],
               'details':'Multiple different quotation numbers refer to this same job; quantities have been added together. Confirm these are add-ons, not replacements: '+docs_desc})
    for ev in docs:
        if not ev['event_date']:
            reviews.append({'level':'URGENT','job':ev['title'],'details':f'Event date missing: {ev["file"]}'})
        if not ev['items']:
            reviews.append({'level':'URGENT','job':ev['title'],'details':f'No items extracted; layout may be unsupported or PDF scanned: {ev["file"]}'})
        if ev['text_len']<80:
            reviews.append({'level':'URGENT','job':ev['title'],'details':f'Little searchable text in {ev["file"]}; enable OCR.'})
        if ev['delivery_date'] and ev['event_date'] and ev['delivery_date']>ev['event_date']:
            reviews.append({'level':'URGENT','job':ev['title'],'details':f'Delivery is dated later than the event: {ev["file"]}'})
    for rec in records.values():
        rec['source_files']=sorted(rec['source_files'])
        rec['document_nos']=sorted(rec['document_nos'])
    return sorted(records.values(), key=lambda r:(r['start'],r['job'],r['item'])),reviews,docs


def as_number(v):
    if v is None:return None
    return int(v) if v==int(v) else float(v)


def iso_time(dt):
    return dt.strftime('%Y-%m-%d %H:%M')


def assess_movement(bookings,stock=None,turnaround_hours=4):
    """Intervals are [delivery, collection+turnaround). Simultaneous demand is
    calculated by sweeping all events, not by summing every event in the month.
    """
    stock=stock or {}
    buffer=timedelta(hours=max(0,float(turnaround_hours)))
    by_item=defaultdict(list)
    for rec in bookings:
        by_item[(norm_key(rec['supplier']),rec['key'])].append(rec)
    pairs=[];requirements=[]
    for (supplier,item_key),jobs in sorted(by_item.items()):
        jobs.sort(key=lambda v:(v['start'],v['job']))
        onhand=stock_for(stock,jobs[0]['supplier'],item_key)
        for idx,a in enumerate(jobs):
            for b in jobs[idx+1:]:
                # For the same physical pieces, return and turnaround must finish before next delivery.
                if not (a['start']<b['end']+buffer and b['start']<a['end']+buffer):continue
                demand=a['qty']+b['qty']
                shortage=max(Decimal(0),demand-onhand) if onhand is not None else None
                pairs.append({
                    'Furniture Item':a['item'],'Supplier':a['supplier'],
                    'Job 1':a['job'],'Job 1 Event Date':a['event_date'],
                    'Job 1 Delivery Date & Time':iso_time(a['start']),
                    'Job 1 Collection Date & Time':iso_time(a['end']),
                    'Job 1 Quantity':as_number(a['qty']),
                    'Job 2':b['job'],'Job 2 Event Date':b['event_date'],
                    'Job 2 Delivery Date & Time':iso_time(b['start']),
                    'Job 2 Collection Date & Time':iso_time(b['end']),
                    'Job 2 Quantity':as_number(b['qty']),
                    'Total Units Required':as_number(demand),
                    'Stock Available':as_number(onhand),
                    'Extra Units Required':as_number(shortage),
                    'Availability':('STOCK UNKNOWN - CONFIRM' if onhand is None else
                                    ('SHORTAGE' if shortage>0 else 'COVERED BY STOCK')),
                    'Time Confirmation Needed':'YES' if a['time_assumed'] or b['time_assumed'] else 'NO',
                    'Job 1 PDF Pages':'; '.join(a['source_pages']),
                    'Job 2 PDF Pages':'; '.join(b['source_pages'])})
        # Sweep every overlapping job to obtain the true peak (incl. 3+ simultaneous jobs).
        changes=[]
        for r in jobs:
            changes.append((r['start'],1,r))
            changes.append((r['end']+buffer,0,r))
        changes.sort(key=lambda x:(x[0],x[1])) # end processed before start
        active={};current=Decimal(0);peak=Decimal(0);at_peak=[];peak_at=None
        for at,kind,r in changes:
            if kind:
                active[r['job_id']]=r;current+=r['qty']
            else:
                active.pop(r['job_id'],None);current-=r['qty']
            if current>peak:
                peak=current;at_peak=list(active.values());peak_at=at
        deficit=max(Decimal(0),peak-onhand) if onhand is not None else None
        requirements.append({
            'Furniture Item':jobs[0]['item'],
            'Supplier':jobs[0]['supplier'],'Total Jobs Booking Item':len(jobs),
            'Jobs at Peak':len(at_peak),
            'Maximum Units Needed At Once':as_number(peak),
            'Stock Available':as_number(onhand),
            'Extra Units Required':as_number(deficit),
            'Peak Demand Starts':iso_time(peak_at) if peak_at else '',
            'Jobs Needing Stock at Peak':'; '.join(x['job']+' ('+format(x['qty'],'g')+' units)' for x in at_peak),
            'Availability':('STOCK UNKNOWN - CONFIRM' if onhand is None else
                            ('SHORTAGE' if deficit>0 else 'COVERED BY STOCK')),
            'Time Confirmation Needed':'YES' if any(j['time_assumed'] for j in at_peak) else 'NO'})
    pairs.sort(key=lambda x:(x['Job 1 Event Date'] or '',x['Furniture Item'],x['Job 2 Event Date'] or ''))
    requirements.sort(key=lambda x:(0 if x['Availability']=='SHORTAGE' else 1,
                                    -x['Maximum Units Needed At Once'],x['Furniture Item']))
    movement=[]
    for r in bookings:
        movement.append({
            'Furniture Item':r['item'],'Supplier':r['supplier'],
            'Job / Event':r['job'],'Event Date':r['event_date'],'Venue':r['venue'],
            'Units for Job':as_number(r['qty']),
            'Scheduled Delivery':iso_time(r['start']),
            'Scheduled Collection':iso_time(r['end']),
            'Ready for Next Delivery (After Buffer)':iso_time(r['end']+buffer),
            'Time Confirmation Needed':'YES' if r['time_assumed'] else 'NO',
            'Source PDFs':'; '.join(r['source_files']),
            'Source Pages':'; '.join(r['source_pages'])})
    return pairs,requirements,movement


def write_rows(path,rows,columns):
    with open(path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
        w.writeheader()
        for r in rows:w.writerow({c:r.get(c,'') if r.get(c) is not None else '' for c in columns})


def build_furniture_outputs(events,bookings,pairs,requirements,movement,reviews,outdir,turnaround_hours=4):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, A3
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    os.makedirs(outdir,exist_ok=True)
    outdir=Path(outdir)
    pair_cols=[
        'Furniture Item','Job 1','Job 1 Event Date','Job 1 Delivery Date & Time',
        'Job 1 Collection Date & Time','Job 1 Quantity','Job 2','Job 2 Event Date',
        'Job 2 Delivery Date & Time','Job 2 Collection Date & Time','Job 2 Quantity',
        'Total Units Required','Stock Available','Extra Units Required','Availability',
        'Supplier','Time Confirmation Needed','Job 1 PDF Pages','Job 2 PDF Pages']
    req_cols=['Furniture Item','Supplier','Total Jobs Booking Item','Jobs at Peak',
        'Maximum Units Needed At Once','Stock Available','Extra Units Required',
        'Peak Demand Starts','Jobs Needing Stock at Peak','Availability','Time Confirmation Needed']
    mov_cols=['Furniture Item','Job / Event','Event Date','Units for Job',
        'Scheduled Delivery','Scheduled Collection','Ready for Next Delivery (After Buffer)',
        'Supplier','Venue','Time Confirmation Needed','Source PDFs','Source Pages']
    event_cols=['Job / Event','Event Date','Venue','Client','Supplier','Delivery Date & Time',
        'Collection Date & Time','Furniture Units Booked','Furniture Item Types',
        'Source PDF','Time Confirmation Needed']
    events_table=[]
    for ev in events:
        lines=[r for r in bookings if ev['file'] in r['source_files']]
        events_table.append({
            'Job / Event':ev['title'],'Event Date':ev['event_date'],
            'Venue':ev['venue'],'Client':ev['customer'],'Supplier':ev['supplier'],
            'Delivery Date & Time':(ev['delivery_date']+' '+ev['delivery_time']).strip(),
            'Collection Date & Time':(
                iso_time(datetime.fromisoformat(ev['window_end']))+' (assumed end-of-day midnight)'
                if ev['collection_midnight'] and ev['window_end']
                else (ev['collection_date']+' '+ev['collection_time']).strip()),
            'Furniture Units Booked':as_number(sum((r['qty'] for r in lines),Decimal(0))),
            'Furniture Item Types':len(lines),'Source PDF':ev['file'],
            'Time Confirmation Needed':'YES' if ev['window_assumed'] or ev['collection_midnight'] else 'NO'})
    files={
        'pair':outdir/'Furniture_Booking_Clashes.csv',
        'stock':outdir/'Furniture_Stock_Needs.csv',
        'movement':outdir/'Furniture_Delivery_Collection_Schedule.csv',
        'events':outdir/'Event_Bookings_Overview.csv',
        'review':outdir/'Booking_Details_To_Confirm.csv',
        'template':outdir/'Furniture_Stock_Count_Template.csv',
        'pdf':outdir/'Furniture_Booking_Clash_Report.pdf'}
    for kind,rows,cols in (('pair',pairs,pair_cols),('stock',requirements,req_cols),
                           ('movement',movement,mov_cols),('events',events_table,event_cols),
                           ('review',reviews,['level','job','details'])):
        write_rows(files[kind],rows,cols)
    # Real stock quantities are NEVER inferred from quotations.
    templates=[{'supplier':r['Supplier'],'item_name':r['Furniture Item'],'available_qty':''}
               for r in requirements]
    write_rows(files['template'],templates,['supplier','item_name','available_qty'])

    pdf=files['pdf']
    page_size=landscape(A3)
    doc=SimpleDocTemplate(str(pdf),pagesize=page_size,
                          leftMargin=32,rightMargin=32,topMargin=38,bottomMargin=34)
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='FmTitle',parent=styles['Title'],fontSize=20,leading=24,
        textColor=HexColor('#102C3F'),spaceAfter=9))
    styles.add(ParagraphStyle(name='FmHeader',parent=styles['Heading2'],fontSize=12,leading=15,
        textColor=HexColor('#0E6671'),spaceBefore=16,spaceAfter=7))
    styles.add(ParagraphStyle(name='FmBody',parent=styles['BodyText'],fontSize=9,leading=12,spaceAfter=6))
    styles.add(ParagraphStyle(name='FmSmall',parent=styles['BodyText'],fontSize=8,leading=10,spaceAfter=0))
    styles.add(ParagraphStyle(name='FmTiny',parent=styles['BodyText'],fontSize=7,leading=9,spaceAfter=0))
    P=lambda value,sty='FmSmall':Paragraph(
        escape(str(value if value is not None and value!='' else '-')).replace('\n','<br/>'),styles[sty])
    story=[Paragraph('FURNITURE BOOKINGS: CLASHES &amp; STOCK NEEDS',styles['FmTitle']),
           P('Compare jobs with overlapping furniture reservations, including each delivery and collection, and the number of units needed.','FmBody'),
           P(f'{len(events)} PDF(s) | {len(set(job_identity(e) for e in events))} jobs | {len(bookings)} furniture allocations | {len(pairs)} item/job clashes | {turnaround_hours:g}h turnaround allowance','FmBody')]
    if any(r['Availability']=='SHORTAGE' for r in requirements):
        story.append(P('ACTION REQUIRED: recorded stock is not enough for all simultaneous jobs.','FmBody'))
    else:
        story.append(P('No verified shortage calculated. Blank stock counts mean UNKNOWN availability, not zero extra units.','FmBody'))
    story.append(P('A furniture item is unavailable from its delivery until its collection PLUS the turnaround buffer. The peak-stock table considers all simultaneous jobs, not just pairs. Midnight and missing times require confirmation. Quoted jobs may not be confirmed bookings.','FmBody'))

    def tab(rows,columns,widths,limit=None):
        body=[[P(c,'FmTiny') for c in columns]]
        for r in (rows if limit is None else rows[:limit]):
            body.append([P(r.get(c),'FmTiny') for c in columns])
        table=Table(body,colWidths=widths,repeatRows=1,hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),HexColor('#DFEDF1')),
            ('TEXTCOLOR',(0,0),(-1,0),HexColor('#173247')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,HexColor('#F7FAFB')]),
            ('LINEBELOW',(0,0),(-1,-1),.3,HexColor('#D9E2E7')),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5)]))
        story.append(table)

    story.append(Paragraph('1. Furniture booking clashes - job-by-job',styles['FmHeader']))
    pair_pdf_cols=[
        'Furniture Item','Job 1','Job 1 Event Date','Job 1 Delivery Date & Time',
        'Job 1 Collection Date & Time','Job 1 Quantity','Job 2','Job 2 Event Date',
        'Job 2 Delivery Date & Time','Job 2 Collection Date & Time','Job 2 Quantity',
        'Total Units Required','Stock Available','Extra Units Required','Time Confirmation Needed']
    pair_widths=[115,94,64,83,83,42,94,64,83,83,42,73,58,73,61]
    if pairs:
        tab(pairs,pair_pdf_cols,pair_widths)
    else:
        # Show the column layout even for an input set containing only one event.
        tab([],pair_pdf_cols,pair_widths)
        story.append(P('No clashes found between separate jobs in these uploads. At least two different jobs are required for a comparison.','FmBody'))
    story.append(Paragraph('2. Furniture stock needed - peak across all jobs',styles['FmHeader']))
    if requirements:
        cols=['Furniture Item','Supplier','Jobs at Peak','Maximum Units Needed At Once',
              'Stock Available','Extra Units Required','Jobs Needing Stock at Peak','Availability']
        tab(requirements,cols,[156,165,100,140,100,120,205,120])
    else:story.append(P('No tracked furniture found. Review your furniture keywords or PDF layout.','FmBody'))
    story.append(Paragraph('3. Delivery & collection calendar - all jobs',styles['FmHeader']))
    cols=['Job / Event','Event Date','Venue','Delivery Date & Time',
          'Collection Date & Time','Furniture Units Booked','Time Confirmation Needed']
    tab(events_table,cols,[208,108,215,155,178,120,122])
    story.append(Paragraph('4. Booking details to confirm',styles['FmHeader']))
    if reviews:
        for x in reviews:
            story.append(P(x['level']+' - '+x['job']+': '+x['details'],'FmBody'))
    else:story.append(P('No date/time extraction flags. Confirm bookings and inventory against source records.','FmBody'))
    story.append(Spacer(1,10))
    story.append(P('CSV exports include full supplier, booking, document/page and movement details. If a time is estimated, check the source PDF before making logistics decisions.','FmBody'))
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',8);canvas.setFillColor(HexColor('#71828C'))
        canvas.drawString(32,18,'Furniture allocation | Confirm booked jobs, collection times and stock counts')
        canvas.drawRightString(page_size[0]-32,18,str(doc.page))
        canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    out_zip=outdir/'Furniture_Booking_Reports.zip'
    with zipfile.ZipFile(out_zip,'w',zipfile.ZIP_DEFLATED) as z:
        for pth in files.values():z.write(pth,arcname=pth.name)
    return {**{k:str(v) for k,v in files.items()},'zip':str(out_zip),
            'pdf_count':len(events),'overlap_count':len(pairs),
            'shortage_count':sum(r['Availability']=='SHORTAGE' for r in requirements)}
