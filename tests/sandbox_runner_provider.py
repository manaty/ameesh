# SPDX-License-Identifier: AGPL-3.0-only
"""Child-process provider double for runner sandbox integration tests."""
import json

from ameesh.sandbox_provider import FakeJobProvider
from ameesh.storage.postgres.sandbox import _canonical


class CompletingProvider(FakeJobProvider):
    def __init__(self, options):
        super().__init__()
        self.input = json.loads(options['secrets']['prompt'])

    def create(self, spec):
        job = super().create(spec)
        data = self.input
        result = dict(schema='ameesh-sandbox-result/1', turn_id=data['turn_id'], epoch=data['epoch'],
                      scope=data['scope'], scope_digest=data['scope_digest'], result_id='result-1',
                      outcome='accepted', verdict='green', findings=[], evidence=[],
                      usage=dict(turns=1, tool_calls=1, seconds=1, model_calls=1),
                      cost={'usd': 0}, sandbox_session=None, source=data['source'],
                      image_digest=spec.image.rsplit('@', 1)[-1], finished_at='2026-10-31T16:00:00Z')
        self.append_log(job.namespace, job.name, json.dumps(result))
        self.set_status(job.namespace, job.name, {'conditions': [{'type': 'Complete', 'status': 'True'}]},
                        message=json.dumps({'result_sha256': _canonical(result)[1]}))
        return self.get(job.namespace, job.name)
