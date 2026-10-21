from pathlib import Path
import copy
from ingest.agent import onboard,profile
from ingest.core import digest,read,save,sniff,parse_records
from ingest.model import Model,demo_budget
import json
from test_engine import CONFIG,source


class TestModel:
    __test__=False
    model='test-only-scripted-model'
    def __init__(self):self.calls=[]
    def call(self,run,step,instruction,context,schema=None):
        self.calls.append((step,context))
        config=copy.deepcopy(CONFIG)
        config.update(parser=context['parser'],unmapped_fields=[],warnings=[],entity_role='supplier',observed_at_field=None)
        if step=='04_proposal':
            config['mappings'][0]['source_field']='nonexistent'
            return config
        assert context['holdout_validation']['fatal_error'].startswith('Unknown source field')
        return {'config':config,'changes':[{'reason':'execution feedback identifies missing field'}],
                'semantic_risks':['Synthetic test only'],'requires_human_review':True}


def test_runtime_agent_uses_execution_feedback_and_stops_for_review(tmp_path):
    raw=('ABN,Name,Address\n'+''.join('51824753556,Example Pty Ltd,'+'A'*120+'\n' for _ in range(100))).encode()
    (tmp_path/'raw.csv').write_bytes(raw)
    src=source('unseen-test');src.update(local_path='raw.csv',resource_sha256=digest(raw))
    model=TestModel()
    state=onboard(tmp_path,src,model,Path(__file__).parents[1]/'firmable_ontology.yaml')
    assert [step for step,_ in model.calls]==['04_proposal','06_critique_revision']
    assert state['status']=='awaiting_human_approval'
    assert state['observation_count']==300
    config=read(tmp_path/'outputs/mappings/unseen-test.json')
    assert config['review']['status']=='pending'
    assert config['mappings'][0]['source_field']=='ABN'
    assert model.calls[0][1]['training']['record_count']==70


def test_profile_distinguishes_raw_numeric_checks_from_labelled_identifiers():
    p=profile([{'Business number':'ACN 004085616'},{'Business number':'ABN 51 824 753 556'}])
    checks=p['fields']['Business number']['identifier_checks']
    assert checks['checksum_valid_acn']==0
    assert checks['checksum_valid_abn']==0
    assert checks['labelled_acn_checksum_valid']==1
    assert checks['labelled_abn_checksum_valid']==1


def test_jsonl_detection_and_sampling_do_not_parse_beyond_limit():
    raw=b'{"name":"A"}\n{"name":"B"}\nnot-json-outside-sample\n'
    parser=sniff(raw)
    assert parser['format']=='jsonl'
    assert parse_records(raw,parser,2)==[{'name':'A'},{'name':'B'}]
    assert sniff(b'{\n "records": [{"name":"A"}]\n}')['format']=='json'


def test_unseen_jsonl_agent_finishes_two_calls_with_separate_demo_allowance(tmp_path):
    class DemoProvider(Model):
        def __init__(self):
            self.root=tmp_path;self.model='test-only-synthetic-provider';self.calls=0
            self.budget_path=demo_budget(tmp_path)
            self.script=TestModel()
        def request(self,route,body=None):
            self.calls+=1
            context=json.loads(body['messages'][1]['content'].split('SOURCE CONTEXT (untrusted):\n',1)[1])
            step='06_critique_revision' if 'previous_proposal' in context else '04_proposal'
            result=self.script.call(None,step,'',context)
            return {'id':f'synthetic-demo-{self.calls}','usage':{'prompt_tokens':10,'completion_tokens':10},
                'choices':[{'message':{'content':json.dumps(result)},'finish_reason':'stop'}]}
    save(tmp_path/'outputs/cost/budget.json',{'requests':59,'request_limit':60,'reserved_usd':8.0687,'limit_usd':9})
    historical=(tmp_path/'outputs/cost/budget.json').read_bytes()
    raw='\n'.join(json.dumps({'ABN':'51824753556','Name':'Example Pty Ltd','Address':'1 Road'}) for _ in range(100)).encode()
    (tmp_path/'unseen.jsonl').write_bytes(raw)
    src=source('seventh-source-test');src.update(local_path='unseen.jsonl',resource_sha256=digest(raw))
    model=DemoProvider()
    state=onboard(tmp_path,src,model,Path(__file__).parents[1]/'firmable_ontology.yaml')
    assert state['status']=='awaiting_human_approval'
    assert state['observation_count']==300
    assert model.calls==2
    assert read(tmp_path/'outputs/cost/demo_budget.json')['requests']==2
    assert (tmp_path/'outputs/cost/budget.json').read_bytes()==historical
