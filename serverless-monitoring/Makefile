.PHONY: test check simulate alarms deploy
test:
	python -m unittest discover -s tests -t . -v
check:
	python tools/check_template.py template.yaml generated/alarms.template.json
simulate:
	python tools/simulate.py
alarms:
	python scripts/generate_alarms.py config/monitoring.example.json > generated/alarms.template.json
deploy:
	sam build && sam deploy --guided
