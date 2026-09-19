# The rig on this machine. Every target is one step of running or updating it.
#
#   make sync        make the venv and install the one dependency
#   make test        run the tests
#   make run         serve over HTTP in the foreground
#   make install     write the LaunchAgent and start it (macOS)
#   make update      git pull, sync, restart: the way to take a new version
#   make restart     restart the running rig
#   make stop        stop the rig and remove the LaunchAgent
#   make status      is it running, does it answer
#   make logs        follow the log
#   make image       build the arm64 image and push it to the registry
#   make deploy      push the image, then roll the Nomad job onto it

PORT    ?= 8101
CONFIG  ?= $(HOME)/.config/bench/bench.json
LABEL   ?= io.kopsa.bench
PLIST   := $(HOME)/Library/LaunchAgents/$(LABEL).plist
LOG     := $(HOME)/Library/Logs/bench.log
PYTHON  := $(CURDIR)/.venv/bin/python
UID     := $(shell id -u)
PATHS   ?= $(HOME)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

IMAGE     ?= ghcr.io/ckopsa/waymark-bench
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)$(shell git diff --quiet HEAD 2>/dev/null || echo -dirty)
PLATFORM  ?= linux/arm64

.PHONY: sync test run install update restart stop status logs plist image deploy

sync:
	uv sync

test: sync
	uv run python -m unittest -v

run: sync
	$(PYTHON) -m bench --http $(PORT) --config $(CONFIG)

plist:
	@mkdir -p $(dir $(PLIST)) $(dir $(LOG))
	@printf '%s\n' \
	'<?xml version="1.0" encoding="UTF-8"?>' \
	'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	'<plist version="1.0">' \
	'<dict>' \
	'  <key>Label</key><string>$(LABEL)</string>' \
	'  <key>ProgramArguments</key>' \
	'  <array>' \
	'    <string>$(PYTHON)</string>' \
	'    <string>-m</string><string>bench</string>' \
	'    <string>--http</string><string>$(PORT)</string>' \
	'    <string>--config</string><string>$(CONFIG)</string>' \
	'  </array>' \
	'  <key>WorkingDirectory</key><string>$(CURDIR)</string>' \
	'  <key>EnvironmentVariables</key>' \
	'  <dict>' \
	'    <key>PATH</key><string>$(PATHS)</string>' \
	'    <key>HOME</key><string>$(HOME)</string>' \
	'  </dict>' \
	'  <key>RunAtLoad</key><true/>' \
	'  <key>KeepAlive</key><true/>' \
	'  <key>StandardOutPath</key><string>$(LOG)</string>' \
	'  <key>StandardErrorPath</key><string>$(LOG)</string>' \
	'</dict>' \
	'</plist>' > $(PLIST)
	@plutil -lint $(PLIST)

install: sync plist
	-launchctl bootout gui/$(UID)/$(LABEL) 2>/dev/null && sleep 1
	launchctl bootstrap gui/$(UID) $(PLIST)
	@sleep 1 && $(MAKE) --no-print-directory status

update:
	git pull --ff-only
	$(MAKE) --no-print-directory sync
	$(MAKE) --no-print-directory restart

restart:
	launchctl kickstart -k gui/$(UID)/$(LABEL)
	@sleep 1 && $(MAKE) --no-print-directory status

stop:
	-launchctl bootout gui/$(UID)/$(LABEL)
	rm -f $(PLIST)

status:
	@launchctl print gui/$(UID)/$(LABEL) 2>/dev/null | grep -E 'state =|pid =' || echo "not loaded"
	@curl -sf http://127.0.0.1:$(PORT)/health || echo "no answer on :$(PORT)"
	@echo

logs:
	tail -f $(LOG)

# The image, as CI builds it (.github/workflows/image.yml): the same
# tag from the same command, so a laptop push and a CI push name one
# image. A cross-build under qemu needs binfmt installed once per boot.
image:
	docker buildx build --platform $(PLATFORM) -t $(IMAGE):$(IMAGE_TAG) --push .
	@echo "pushed $(IMAGE):$(IMAGE_TAG)"

# The deploy is one Nomad variable; the job template reads it and
# restarts the task on the new tag. NOMAD_ADDR and NOMAD_TOKEN come
# from the environment.
deploy: image
	nomad var put -force nomad/jobs/waymark-bench/deploy image_tag=$(IMAGE_TAG) >/dev/null
	@echo "deploying $(IMAGE):$(IMAGE_TAG)"
