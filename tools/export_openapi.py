#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema to frontend/openapi.json.

The frontend generates its TypeScript types from this file
(`npm run typegen` → src/api/types.gen.ts). CI re-runs both and fails
on drift, so the schema committed here always matches the server.

Usage: python tools/export_openapi.py [output-path]
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def main():
    out = (sys.argv[1] if len(sys.argv) > 1 else
           os.path.join(os.path.dirname(__file__), '..', 'frontend',
                        'openapi.json'))

    from pydantic.json_schema import models_json_schema

    from perflens.api import models
    from perflens.app import AppContext
    from perflens.config import ServerConfig
    from perflens.state import MetricsState, ProfilingState
    from perflens.web import create_app

    ctx = AppContext(config=ServerConfig(ui_dir='/nonexistent'),
                     state=ProfilingState(max_samples=1),
                     metrics=MetricsState())
    app = create_app(ctx)
    schema = app.openapi()

    # The SSE payload and agent-command models aren't referenced by any
    # REST route — merge them into components so TS types generate for
    # them too.
    extra = [models.SSECatalog, models.AgentCommand, models.AgentHello,
             models.StartArgs, models.ConfigureArgs,
             models.ConfigureMetricsArgs]
    _, defs = models_json_schema(
        [(m, 'validation') for m in extra],
        ref_template='#/components/schemas/{model}')
    schema.setdefault('components', {}).setdefault('schemas', {}).update(
        defs.get('$defs', {}))

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(schema, f, indent=2, sort_keys=True)
        f.write('\n')
    n_paths = len(schema.get('paths', {}))
    n_schemas = len(schema.get('components', {}).get('schemas', {}))
    print(f'wrote {out}: {n_paths} paths, {n_schemas} schemas')


if __name__ == '__main__':
    main()
