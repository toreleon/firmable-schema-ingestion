import json
from ingest.core import read,save
from ingest.model import Model,demo_budget
import pytest


class SyntheticProvider(Model):
    def __init__(self,root):
        self.root=root;self.model='synthetic-test-provider';self.called=False
    def request(self,route,body=None):
        self.called=True
        return {'id':'synthetic-response','usage':{'prompt_tokens':10,'completion_tokens':10},
                'choices':[{'message':{'content':'invalid JSON'},'finish_reason':'stop'}]}


def test_exhausted_budget_prevents_provider_request(tmp_path):
    save(tmp_path/'outputs/cost/budget.json',{'requests':1,'request_limit':60,'reserved_usd':9,'limit_usd':9})
    model=SyntheticProvider(tmp_path)
    with pytest.raises(RuntimeError,match='budget'):
        model.call(tmp_path/'run','test','instruction',{})
    assert not model.called


def test_billable_usage_survives_invalid_model_json(tmp_path):
    model=SyntheticProvider(tmp_path)
    with pytest.raises(json.JSONDecodeError):
        model.call(tmp_path/'run','test','instruction',{})
    assert read(tmp_path/'run/test.provider_response.json')['usage']['completion_tokens']==10
    assert read(tmp_path/'outputs/cost/budget.json')['reserved_usd']>0


def test_truncated_valid_json_is_never_accepted_from_legacy_cache(tmp_path):
    class TruncatedThenComplete(SyntheticProvider):
        def __init__(self,root):super().__init__(root);self.calls=0
        def request(self,route,body=None):
            self.calls+=1
            return {'id':f'synthetic-{self.calls}','usage':{'prompt_tokens':10,'completion_tokens':10},
                'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'length' if self.calls==1 else 'stop'}]}
    model=TruncatedThenComplete(tmp_path)
    with pytest.raises(ValueError,match='truncated'):
        model.call(tmp_path/'run','test','instruction',{})
    assert not (tmp_path/'run/test.model.json').exists()
    assert read(tmp_path/'run/test.error.json')['usage_unknown'] is False
    fingerprint=read(tmp_path/'run/test.error.json')['request_sha256']
    save(tmp_path/'run/test.model.json',{'request_sha256':fingerprint,'parsed':{'bad':'legacy truncated'},'finish_reason':'length'})
    assert model.call(tmp_path/'run','test','instruction',{})=={'ok':True}
    assert model.calls==2
    assert model.call(tmp_path/'run','test','instruction',{})=={'ok':True}
    assert model.calls==2
    assert len(list((tmp_path/'run/history').glob('*.usage.json')))==2


def test_demo_allowance_is_separate_bounded_and_cannot_reset(tmp_path):
    save(tmp_path/'outputs/cost/budget.json',{'requests':59,'request_limit':60,'reserved_usd':8.0687,'limit_usd':9})
    historical=(tmp_path/'outputs/cost/budget.json').read_bytes()
    path=demo_budget(tmp_path)
    assert read(path)['request_limit']==6
    assert read(path)['limit_usd']==1
    budget=read(path);budget['requests']=6;save(path,budget)
    assert read(demo_budget(tmp_path))['requests']==6
    model=SyntheticProvider(tmp_path);model.budget_path=path
    with pytest.raises(RuntimeError,match='budget'):
        model.call(tmp_path/'run','test','instruction',{})
    assert not model.called
    with pytest.raises(ValueError,match='immutable'):
        demo_budget(tmp_path,request_limit=7)
    with pytest.raises(ValueError,match='exceed'):
        demo_budget(tmp_path,limit_usd=2)
    with pytest.raises(ValueError,match='positive'):
        demo_budget(tmp_path,limit_usd=float('nan'))
    assert (tmp_path/'outputs/cost/budget.json').read_bytes()==historical
