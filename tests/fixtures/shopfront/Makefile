.PHONY: test codegen

test:
	PYTHONPATH=src DATABASE_URL=sqlite:///:memory: pytest -q

codegen:
	python tools/codegen.py api/openapi.yaml > src/shopfront/api/schema_gen.py
