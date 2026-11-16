.PHONY: build vet run demo visual-acceptance
build:
	go build ./...
vet:
	go vet ./...
run:
	go run ./cmd/atlas
demo:
	python3 scripts/walkthrough.py --workflow submit-render --out var/demo
visual-acceptance:
	python3 -I scripts/visual_acceptance.py
