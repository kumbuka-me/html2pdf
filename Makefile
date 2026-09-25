# Makefile

.DEFAULT_GOAL := help

LOCALBIN ?= bin

$(LOCALBIN):
	@mkdir -p "$@"

## Tool Versions
# renovate: datasource=github-releases depName=gi8lino/dev-tools
DEV_TOOLS_VERSION ?= v0.9.0

## Tool Binaries
DEV_TOOL_NAMES := dev-port open-browser dev-tag make-help go-install-tool
DEV_TOOL_TARGETS := $(addprefix $(LOCALBIN)/,$(DEV_TOOL_NAMES))
DEV_TOOL_VERSIONED := $(addsuffix -$(DEV_TOOLS_VERSION),$(DEV_TOOL_TARGETS))

DEV_PORT := $(LOCALBIN)/dev-port
OPEN_BROWSER := $(LOCALBIN)/open-browser
DEV_TAG_TOOL := $(LOCALBIN)/dev-tag
MAKE_HELP := $(LOCALBIN)/make-help
GO_INSTALL_TOOL := $(LOCALBIN)/go-install-tool

# Run a local tool while displaying only its executable name.
define run-tool
@printf '%s\n' '$(notdir $(1)) $(2)'
@$(1) $(2)
endef


## Container Configuration
IMAGE ?= html2pdf
COMPOSE_FILE ?= deploy/compose.yaml
DEV_TAG ?= dev
BUILD_VERSION ?= $(shell git describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo dev)
HOST ?= 127.0.0.1
PORT ?= 8080
HTML2PDF__TOKEN ?=
DOCKER_BUILD_ARGS ?=
DOCKER_RUN_ARGS ?=

# Default tag prefix. Override with an empty value for unprefixed tags.
VERSION_PREFIX ?= v

##@ Tagging

.PHONY: current
current: $(DEV_TAG_TOOL) ## Show the current semantic version tag.
	$(call run-tool,$(DEV_TAG_TOOL),--prefix "$(VERSION_PREFIX)" current)

.PHONY: patch
patch: $(DEV_TAG_TOOL) ## Create a new patch release (x.y.Z+1).
	$(call run-tool,$(DEV_TAG_TOOL),--prefix "$(VERSION_PREFIX)" patch)

.PHONY: minor
minor: $(DEV_TAG_TOOL) ## Create a new minor release (x.Y+1.0).
	$(call run-tool,$(DEV_TAG_TOOL),--prefix "$(VERSION_PREFIX)" minor)

.PHONY: major
major: $(DEV_TAG_TOOL) ## Create a new major release (X+1.0.0).
	$(call run-tool,$(DEV_TAG_TOOL),--prefix "$(VERSION_PREFIX)" major)

.PHONY: tag
tag: current

.PHONY: push
push: ## Push tags to the configured remote.
	git push --tags

##@ Development

.PHONY: test
test: ## Run the unit tests.
	python3 -m unittest discover -s tests -v

.PHONY: build
build: ## Build the development container image.
	docker build $(DOCKER_BUILD_ARGS) --build-arg VERSION="$(BUILD_VERSION)" -t $(IMAGE):$(DEV_TAG) .

.PHONY: dev
dev: build ## Build and run the service locally.
	docker run --rm $(DOCKER_RUN_ARGS) \
		-p $(HOST):$(PORT):8080 \
		$(IMAGE):$(DEV_TAG)

.PHONY: dev-auth
dev-auth: build ## Build and run locally with bearer-token authentication.
	@test -n "$(HTML2PDF__TOKEN)" || { echo "Set HTML2PDF__TOKEN first" >&2; exit 1; }
	docker run --rm $(DOCKER_RUN_ARGS) \
		-p $(HOST):$(PORT):8080 \
		-e HTML2PDF__TOKEN="$(HTML2PDF__TOKEN)" \
		$(IMAGE):$(DEV_TAG)

.PHONY: compose
compose: ## Run the development stack with Docker Compose.
	HTML2PDF__BUILD_VERSION="$(BUILD_VERSION)" docker compose -f "$(COMPOSE_FILE)" up --build

.PHONY: compose-auth
compose-auth: ## Run the development stack with bearer-token authentication.
	@test -n "$(HTML2PDF__TOKEN)" || { echo "Set HTML2PDF__TOKEN first" >&2; exit 1; }
	HTML2PDF__BUILD_VERSION="$(BUILD_VERSION)" HTML2PDF__TOKEN="$(HTML2PDF__TOKEN)" docker compose -f "$(COMPOSE_FILE)" up --build

.PHONY: stop
stop: ## Stop the Docker Compose stack.
	docker compose -f "$(COMPOSE_FILE)" down

.PHONY: clean
clean: ## Remove the development container image.
	-docker image rm $(IMAGE):$(DEV_TAG)

##@ General

.PHONY: help
help: $(MAKE_HELP) ## Display this help.
	@$(MAKE_HELP) $(MAKEFILE_LIST)

##@ Development tools

.PHONY: dev-tools
dev-tools: $(DEV_TOOL_TARGETS) ## Download the pinned development tools.

$(DEV_TOOL_TARGETS): $(LOCALBIN)/%: $(LOCALBIN)/%-$(DEV_TOOLS_VERSION)
	@ln -sf "$(notdir $<)" "$@"

$(DEV_TOOL_VERSIONED): $(LOCALBIN)/%-$(DEV_TOOLS_VERSION): | $(LOCALBIN)
	$(call download-dev-tool,$*,$@)

# download-dev-tool downloads a versioned tool from gi8lino/dev-tools.
# $1 - release asset name
# $2 - versioned destination path
define download-dev-tool
	@set -eu; \
	tmp="$(2).tmp"; \
	trap 'rm -f "$$tmp"' EXIT INT TERM; \
	echo "Downloading gi8lino/dev-tools $(DEV_TOOLS_VERSION) $(1)"; \
	curl --fail --silent --show-error --location \
		"https://github.com/gi8lino/dev-tools/releases/download/$(DEV_TOOLS_VERSION)/$(1)" \
		-o "$$tmp"; \
	chmod +x "$$tmp"; \
	mv "$$tmp" "$(2)"; \
	trap - EXIT INT TERM
endef
