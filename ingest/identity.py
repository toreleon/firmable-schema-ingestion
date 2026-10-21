"""Generic raw-record identity checks; these are not additional canonical claims.

Mapping candidates supply name fields even below the canonical confidence floor.
Conservative heading recognition supplies extra negative evidence. No source IDs,
business names, or reviewed record IDs appear in this policy.
"""
import re
import unicodedata
from .core import digest,fields_of,getpath,record_id

CONTEXT_VERSION='raw-subject-context-v1'
CORPORATE=re.compile(r'\b(?:pty|ltd|limited|proprietary|corporation|incorporated)\b',re.I)
TRUSTEE=re.compile(r'\b(?:trustee|atf|a\s+t\s+f|as\s+trustee\s+for)\b',re.I)
TRUST=re.compile(r'\btrust\b',re.I)


def heading(text):
    text=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',str(text))
    return ' '.join(re.sub(r'[^a-z0-9]+',' ',text.casefold()).split())


def name_key(text):
    text=unicodedata.normalize('NFKD',str(text)).casefold()
    text=''.join(c for c in text if not unicodedata.combining(c))
    text=re.sub(r'[^a-z0-9 ]',' ',text)
    text=re.sub(r'\b(?:pty|ltd|limited|proprietary)\b',' ',text)
    return ' '.join(text.split())


def _subject_name_heading(field):
    text=heading(field)
    if re.search(r'\b(?:agency|buyer|contact|project|contract|arrangement|department|officer)\b',text):
        return False
    return text in {'name','legal name','trading name','permit holder s','permit holders'} or bool(re.fullmatch(
        r'(?:supplier|vendor|licensee|licence holder|license holder|customer|operator|business|company|entity|recipient|permit holder)(?: legal| trading| business)? name',text))


def _subject_type_heading(field):
    return bool(re.fullmatch(r'(?:entity|legal entity|business|organisation|organization|licensee|licence holder|license holder|licence|license) type(?: desc| description)?',heading(field)))


def describe(names,types,role='unknown',scope='one_raw_record'):
    type_text=' '.join(str(t['value']) for t in types).casefold()
    raw_names=[str(n['value']).strip() for n in names if str(n['value']).strip()]
    text=' '.join(raw_names)
    person=bool(re.search(r'\bindividual\b|\bsole[ _-]?trader\b|\bnatural person\b',type_text))
    explicit_company=bool(re.search(r'\bcompany\b|\bcorporation\b|\bincorporated\b',type_text))
    trustee=bool(TRUSTEE.search(text))
    trust=trustee or bool(TRUST.search(text)) or 'trust' in type_text
    compound=any(';' in n or '\n' in n for n in raw_names)
    company=explicit_company or any(CORPORATE.search(n) for n in raw_names)
    if person and company:
        kind='ambiguous'
    elif trust:
        kind='trust'
    elif person:
        kind='person_or_sole_trader'
    elif company:
        kind='company'
    elif re.search(r'\bpartnership\b',type_text):
        kind='partnership'
    elif re.search(r'\bgovernment\b',type_text):
        kind='government'
    else:
        kind='unknown'
    return {'context_version':CONTEXT_VERSION,'names':names,'subject_type_evidence':types,
        'name_keys':sorted({name_key(n) for n in raw_names if name_key(n)}),
        'subject_kind':kind,'capacity':'trustee_or_trust' if trust else 'own_capacity_or_unspecified',
        'compound_names':compound,'entity_role':role,'scope':scope,
        'interpretation':'Raw source assertions and conservative checks; no inferred legal name, entity type, or relationship is emitted.'}


def record_context(row,config,source,index):
    mappings=config.get('mappings',[])
    name_fields={m['source_field']:m for m in mappings if m['canonical_field'] in {'entity.legal_name','entity.trading_name'}}
    type_fields={m['source_field'] for m in mappings if m['canonical_field']=='entity.entity_type'}
    names=[];types=[]
    for field in sorted(fields_of(row)):
        val=getpath(row,field)
        if val is None or not isinstance(val,(str,int,float)) or not str(val).strip():
            continue
        if field in name_fields or _subject_name_heading(field):
            names.append({'source_field':field,'value':str(val),'basis':'mapping_candidate' if field in name_fields else 'generic_subject_heading',
                          'canonical_mapping_confidence':name_fields.get(field,{}).get('confidence')})
        if field in type_fields or _subject_type_heading(field):
            types.append({'source_field':field,'value':str(val)})
    result=describe(names,types,config.get('entity_role','unknown'))
    result.update(source_id=source['source_id'],source_record_id=record_id(source,index,row),
                  provenance={'resource_sha256':source['resource_sha256'],'raw_record_sha256':digest(row),'row_number':index})
    return result


def canonical_context(record):
    """Fallback for library callers without raw-context records, including tests."""
    names=[];types=[]
    for observation in record['observations']:
        field=observation['canonical_field'];group,key=field.split('.')
        if field in {'entity.legal_name','entity.trading_name'}:
            names.append({'source_field':observation['provenance']['source_field'],'value':observation[group][key],'basis':'canonical_observation'})
        elif field=='entity.entity_type':
            types.append({'source_field':observation['provenance']['source_field'],'value':observation[group][key]})
    return describe(names,types)


def check_pair(a,b):
    """Return a refusal reason or None. Identifier equality is checked separately."""
    if a['subject_kind']=='ambiguous' or b['subject_kind']=='ambiguous':
        return 'contradictory_subject_type_evidence'
    kinds={a['subject_kind'],b['subject_kind']}
    if 'person_or_sole_trader' in kinds and 'company' in kinds:
        return 'person_company_subject_mismatch'
    if a['capacity']!=b['capacity']:
        return 'trustee_or_trust_capacity_mismatch'
    if a['subject_kind']!='unknown' and b['subject_kind']!='unknown' and a['subject_kind']!=b['subject_kind']:
        return 'incompatible_subject_types'
    if a['compound_names'] or b['compound_names']:
        return 'compound_names_without_safe_party_assignment'
    if not a['name_keys'] or not b['name_keys']:
        return 'missing_subject_name_corroboration'
    if not set(a['name_keys']) & set(b['name_keys']):
        return 'raw_subject_names_disagree'
    return None


def alias_eligible(context):
    # Employee/employer and trustee identifiers must not establish a namespace bridge.
    return (context['subject_kind']=='company' and context['capacity']=='own_capacity_or_unspecified'
            and bool(context['name_keys']) and not context['compound_names'])
