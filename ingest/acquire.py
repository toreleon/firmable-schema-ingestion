"""Resource acquisition only. No field mappings or source-specific record parsing."""
from pathlib import Path
import gzip
from urllib.parse import urlencode
import json
from .core import digest, fetch, load_jsonl, now, read, save


def acquire(root, dataset_id, resource_id, representation='original', limit=1000):
    root = Path(root)
    pool = load_jsonl(root/'data/catalogue_pool.jsonl')
    package = next(p for p in pool if p['id'] == dataset_id)
    resource = next(r for r in package['resources'] if r['id'] == resource_id)
    original = resource['url']
    if representation == 'json_sample':
        import re
        if not re.fullmatch(r'[0-9a-f-]{36}',resource_id):
            raise ValueError('Only UUID DataStore resource identifiers accepted')
        sql = f'SELECT * FROM "{resource_id}" ORDER BY md5(_id::text) LIMIT {int(limit)}'
        url = package['_portal']+'datastore_search_sql?'+urlencode({'sql':sql})
    elif representation == 'json':
        url = package['_portal']+'datastore_search?'+urlencode({'resource_id':resource_id,'limit':limit,'sort':'_id asc'})
    elif representation == 'csv':
        url = package['_portal'].split('/api/')[0]+'/datastore/dump/'+resource_id+'?format=csv'
    else:
        url = original
    path = root/'data/raw'/dataset_id
    source_path = root/'data/sources'/f'{dataset_id}.json'
    if path.exists() and path.stat().st_size and source_path.exists():
        previous = read(source_path)
        if previous['resource_url'] == url:
            return previous
    raw, headers = fetch(url)
    if representation in {'json','json_sample'}:
        reply = json.loads(raw)
        if not reply.get('success') or not reply.get('result',{}).get('records'):
            raise ValueError('Empty or unsuccessful DataStore query')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(raw)
    snapshot = root/'data/snapshots'/f'{dataset_id}.gz'
    snapshot.parent.mkdir(parents=True,exist_ok=True)
    snapshot.write_bytes(gzip.compress(raw,mtime=0))
    source = {'source_id':dataset_id,'dataset_id':dataset_id,'title':package['title'],
              'resource_id':resource_id,'resource_url':url,'original_resource_url':original,
              'acquisition_representation':representation, 'original_format':resource.get('format'),
              'resource_sha256':digest(raw),'local_path':str(path.relative_to(root)),
              'snapshot_path':str(snapshot.relative_to(root)),
              'downloaded_at':now(),'observed_at':resource.get('last_modified') or package.get('metadata_modified'),
              'observed_at_basis':'resource_last_modified_proxy' if resource.get('last_modified') else 'catalogue_metadata_modified_proxy',
              'licence':package.get('license_title') or 'unknown','licence_url':package.get('license_url'),
              'publisher':(package.get('organization') or {}).get('title'),
              'source_reliability':.8,'source_reliability_basis':'Uncalibrated government-source prior; uniform, not claimed measured',
              'description':package.get('notes','')[:4000],
              'temporal_warning':'Resource/catalogue modification time is a publication proxy, not a verified record statement time',
              'licence_warning':not bool(package.get('license_url'))}
    save(source_path,source)
    return source
