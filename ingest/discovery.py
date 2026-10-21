"""Catalogue metadata triage. Scores are ranking heuristics, not calibrated probabilities."""
import random
import re
from pathlib import Path
from urllib.parse import urlencode
from .core import fetch, now, save, jsonl,load_jsonl
import json

PORTALS = ['https://www.data.qld.gov.au/api/3/action/', 'https://data.nsw.gov.au/data/api/3/action/']
QUERIES = ['abn', 'contract', 'licence', 'supplier', 'business register', 'grant', 'charity']
SIGNALS = [(r'\babn\b|australian business number', 5, 'mentions ABN'),
           (r'\bacn\b|australian company number', 4, 'mentions ACN'),
           (r'supplier|vendor|contractor', 4, 'names business suppliers'),
           (r'contract|procurement', 3, 'procurement records'),
           (r'licen[sc]e|register|registration', 2, 'licensing/register records'),
           (r'company|companies|businesses|charit', 2, 'business or charity entities'),
           (r'grant|recipient|tender', 2, 'funding recipients'),
           (r'aggregate|statistic|counts|survey|forecast|boundary|boundaries|zoning|land use', -3, 'aggregate/geographic content risk')]


def score(package):
    text = ' '.join([package.get('title',''), package.get('notes','')[:12000], ' '.join(t.get('name','') for t in package.get('tags',[]))]).casefold()
    total, reasons = 0, []
    for pattern, weight, reason in SIGNALS:
        if re.search(pattern, text):
            total += weight
            reasons.append(reason)
    title = package.get('title','').casefold()
    if re.search(r'\bregister\b|\bdirectory\b|registered operators',title):
        total += 5
        reasons.append('title indicates record-level register/directory')
    if re.search(r'\bcontract disclosure\b|\bcontracts disclosure\b',title):
        total += 2
    if 'procurement' in title:
        total += 3
        reasons.append('title explicitly identifies procurement records')
    if re.search(r'vegetation|ecological|geological|surveys|statistic|customer check.ins|annual activity report|final report for period',title):
        total -= 6
        reasons.append('title indicates reporting/aggregate/geographic content risk')
    if not package.get('resources'):
        total -= 6
        reasons.append('no downloadable catalogue resources')
    formats = sorted({r.get('format','').upper() for r in package.get('resources',[])})
    if set(formats) & {'CSV','XLSX','JSON','XML','XLS','GEOJSON','ZIP'}:
        total += 2
    return total, min(.98, max(.05, .35 + total*.035)), '; '.join(reasons) or 'weak business-related catalogue match', formats


def discover(root, rows=100):
    root = Path(root)
    pool, requests = {}, []
    # Retain the national catalogue failure as evidence; do not retry indefinitely.
    portals = ['https://data.gov.au/data/api/3/action/'] + PORTALS
    for portal in portals:
        for query in (QUERIES if portal in PORTALS else ['abn']):
            url = portal + 'package_search?' + urlencode({'q':query, 'rows':rows, 'start':0})
            try:
                raw, _ = fetch(url)
                reply = json.loads(raw)
                if not reply.get('success'):
                    raise ValueError('CKAN action unsuccessful')
                result = reply['result']
                for p in result['results']:
                    p['_portal'] = portal
                    pool[portal + p['id']] = p
                requests.append({'url':url, 'at':now(), 'returned':len(result['results']), 'total':result['count']})
            except Exception as exc:
                requests.append({'url':url, 'at':now(), 'error':str(exc)})
    packages = list(pool.values())
    jsonl(root/'data/catalogue_pool.jsonl', packages)
    save(root/'outputs/discovery_requests.json', requests)
    return rank_pool(root)


def rank_pool(root):
    root = Path(root)
    packages = load_jsonl(root/'data/catalogue_pool.jsonl')
    if len(packages) < 200:
        raise RuntimeError(f'Only {len(packages)} distinct records retrieved; cannot meet discovery requirement')
    ranked = []
    for p in packages:
        points, confidence, reason, formats = score(p)
        ranked.append({'dataset_id':p['id'], 'title':p['title'],
                       'publishing_organisation':(p.get('organization') or {}).get('title','unknown'),
                       'resource_formats':formats, 'confidence':confidence, 'ranking_score':points,
                       'reason':reason, 'portal':p['_portal'], 'licence':p.get('license_title') or 'unknown',
                       'licence_url':p.get('license_url'), 'score_type':'uncalibrated_metadata_heuristic'})
    ranked.sort(key=lambda r:(-r['ranking_score'], r['dataset_id']))
    shortlist = ranked[:50]
    jsonl(root/'outputs/shortlist.jsonl', shortlist)
    sample = random.Random(20261021).sample(shortlist,20)
    jsonl(root/'outputs/triage_audit.jsonl', [dict(r,contains_business_entities=None,reviewer=None,evidence_url=None,review_notes=None) for r in sample])
    save(root/'outputs/discovery_summary.json', {'pool_size':len(packages), 'shortlist_size':len(shortlist), 'audit_seed':20261021,
         'false_positive_rate':None, 'audit_status':'pending independent human review',
         'score_interpretation':'Ordinal, uncalibrated metadata score. No measured probability or precision claim.'})
    return shortlist
