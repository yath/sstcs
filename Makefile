.PHONY: all lint

all:
	@echo "I can only lint :("
	@exit 1

lint:
	pylint --max-line-length=100 \
		   --disable=bad-whitespace,missing-docstring,invalid-name,attribute-defined-outside-init,fixme \
		   --ignored-modules=twisted.internet.reactor \
		   --dummy-variables-rgx='.*_$$' \
		   sstcs.py
