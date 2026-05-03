PYTHON ?= python3
REGION ?= us-sea
export PYTHONPATH := src
RELEASE_VERSION = $(patsubst v%,%,$(VERSION))
RELEASE_TAG = v$(RELEASE_VERSION)

.PHONY: check test security-check smoke check-gh-env release-notes release-check release-recover release-create-from-tag release-publish

check: security-check test

test:
	$(PYTHON) -m unittest discover -s tests/unit -p 'test_*.py'

security-check:
	$(PYTHON) -m linode_image_lab.validation .

smoke:
	@if [ -z "$${LINODE_TOKEN:-}" ]; then \
		echo "Error: LINODE_TOKEN is required for make smoke." >&2; \
		exit 1; \
	fi
	@if [ "$(SMOKE_EXECUTE)" != "1" ]; then \
		echo "Error: SMOKE_EXECUTE=1 is required for make smoke." >&2; \
		echo "This target is manual-only and creates real Linode resources." >&2; \
		exit 1; \
	fi
	@echo "WARNING: This will create and delete temporary Linodes"
	linode-image-lab capture-deploy --config examples/config/capture-deploy-smoke.toml --region "$(REGION)" --execute

check-gh-env:
	@command -v gh >/dev/null 2>&1 || { echo "Error: GitHub CLI (gh) is required but is not installed." >&2; exit 1; }
	@gh auth status >/dev/null 2>&1 || { echo "Error: GitHub CLI authentication is required. Run 'gh auth login' and try again." >&2; exit 1; }

release-notes:
	@if [ -z "$(VERSION)" ]; then \
		echo "Error: VERSION is required. Usage: make release-publish VERSION=0.1.0" >&2; \
		exit 1; \
	fi
	@if ! printf '%s\n' '$(RELEASE_VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "Error: VERSION must be X.Y.Z or vX.Y.Z." >&2; \
		exit 1; \
	fi
	@awk -v version='$(RELEASE_VERSION)' '\
		BEGIN { found = 0; printed = 0 } \
		$$0 == "## " version { found = 1; next } \
		found && /^## / { exit } \
		found { print; if ($$0 !~ /^[[:space:]]*$$/) printed = 1 } \
		END { \
			if (!found) { \
				printf "Error: CHANGELOG.md missing section ## %s.\n", version > "/dev/stderr"; \
				exit 1; \
			} \
			if (!printed) { \
				printf "Error: CHANGELOG.md section ## %s has no release notes.\n", version > "/dev/stderr"; \
				exit 1; \
			} \
		}' CHANGELOG.md

release-check: check-gh-env
	@if [ -z "$(VERSION)" ]; then \
		echo "Error: VERSION is required. Usage: make release-publish VERSION=0.1.0" >&2; \
		exit 1; \
	fi
	@if ! printf '%s\n' '$(RELEASE_VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "Error: VERSION must be X.Y.Z or vX.Y.Z." >&2; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory release-notes VERSION='$(RELEASE_VERSION)' >/dev/null
	@if [ "$$(git branch --show-current)" != "main" ]; then \
		echo "Error: release must run from main after the release PR is merged." >&2; \
		exit 1; \
	fi
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "Error: working tree must be clean before release." >&2; \
		git status --short; \
		exit 1; \
	fi
	@git fetch origin main >/dev/null
	@if [ "$$(git rev-parse HEAD)" != "$$(git rev-parse origin/main)" ]; then \
		echo "Error: local main must match origin/main. Run 'git pull --ff-only origin main' and retry." >&2; \
		exit 1; \
	fi
	@echo "Running validation: make check"
	@if $(MAKE) check; then \
		echo "Validation passed: make check completed successfully."; \
	else \
		status=$$?; \
		echo "Validation failed: make check exited $$status." >&2; \
		echo "Error: release blocked until canonical validation passes." >&2; \
		exit $$status; \
	fi
	@if git rev-parse --verify --quiet "refs/tags/$(RELEASE_TAG)" >/dev/null; then \
		echo "Error: local tag $(RELEASE_TAG) already exists." >&2; \
		exit 1; \
	fi
	@remote_tag_status=0; \
	git ls-remote --exit-code --tags origin "refs/tags/$(RELEASE_TAG)" >/dev/null 2>&1 || remote_tag_status=$$?; \
	if [ "$$remote_tag_status" -eq 0 ]; then \
		echo "Error: remote tag $(RELEASE_TAG) already exists." >&2; \
		exit 1; \
	elif [ "$$remote_tag_status" -ne 2 ]; then \
		echo "Error: unable to check remote tag $(RELEASE_TAG)." >&2; \
		exit 1; \
	fi
	@release_view=$$(gh release view "$(RELEASE_TAG)" 2>&1 >/dev/null); \
	release_status=$$?; \
	if [ "$$release_status" -eq 0 ]; then \
		echo "Error: GitHub release $(RELEASE_TAG) already exists." >&2; \
		exit 1; \
	elif ! printf '%s\n' "$$release_view" | grep -Eiq 'not found|could not find'; then \
		echo "Error: unable to check GitHub release $(RELEASE_TAG): $$release_view" >&2; \
		exit 1; \
	fi
	@echo "Release checks passed for $(RELEASE_TAG)."

release-recover: check-gh-env
	@if [ -z "$(VERSION)" ]; then \
		echo "Error: VERSION is required. Usage: make release-recover VERSION=0.1.0" >&2; \
		exit 1; \
	fi
	@if ! printf '%s\n' '$(RELEASE_VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "Error: VERSION must be X.Y.Z or vX.Y.Z." >&2; \
		exit 1; \
	fi
	@local_tag_state=missing; \
	if git rev-parse --verify --quiet "refs/tags/$(RELEASE_TAG)" >/dev/null; then \
		local_tag_state=exists; \
	fi; \
	remote_tag_state=missing; \
	remote_tag_status=0; \
	git ls-remote --exit-code --tags origin "refs/tags/$(RELEASE_TAG)" >/dev/null 2>&1 || remote_tag_status=$$?; \
	if [ "$$remote_tag_status" -eq 0 ]; then \
		remote_tag_state=exists; \
	elif [ "$$remote_tag_status" -eq 2 ]; then \
		remote_tag_state=missing; \
	else \
		echo "Error: unable to inspect remote tag $(RELEASE_TAG)." >&2; \
		echo "No automatic recovery action was taken." >&2; \
		exit 1; \
	fi; \
	release_state=missing; \
	release_view=$$(gh release view "$(RELEASE_TAG)" 2>&1 >/dev/null); \
	release_status=$$?; \
	if [ "$$release_status" -eq 0 ]; then \
		release_state=exists; \
	elif printf '%s\n' "$$release_view" | grep -Eiq 'not found|could not find'; then \
		release_state=missing; \
	else \
		echo "Error: unable to inspect GitHub release $(RELEASE_TAG): $$release_view" >&2; \
		echo "No automatic recovery action was taken." >&2; \
		exit 1; \
	fi; \
	echo "Release recovery state for $(RELEASE_TAG):"; \
	echo "  local tag: $$local_tag_state"; \
	echo "  remote tag: $$remote_tag_state"; \
	echo "  GitHub release: $$release_state"; \
	if [ "$$release_state" = "exists" ]; then \
		echo "GitHub release $(RELEASE_TAG) already exists; normal recovery may be complete."; \
	elif [ "$$remote_tag_state" = "exists" ]; then \
		echo "Remote tag $(RELEASE_TAG) has already been published, but the GitHub release is missing."; \
		echo "To create the missing release from the existing remote tag, run:"; \
		echo "  make release-create-from-tag VERSION=$(RELEASE_VERSION)"; \
	elif [ "$$local_tag_state" = "exists" ]; then \
		echo "Local tag $(RELEASE_TAG) exists, but the remote tag and GitHub release are missing."; \
		echo "If the publish should be retried from scratch, delete only the local tag manually:"; \
		echo "  git tag -d $(RELEASE_TAG)"; \
	else \
		echo "No partial release publish state was found for $(RELEASE_TAG)."; \
	fi

release-create-from-tag: check-gh-env
	@if [ -z "$(VERSION)" ]; then \
		echo "Error: VERSION is required. Usage: make release-create-from-tag VERSION=0.1.0" >&2; \
		exit 1; \
	fi
	@if ! printf '%s\n' '$(RELEASE_VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "Error: VERSION must be X.Y.Z or vX.Y.Z." >&2; \
		exit 1; \
	fi
	@remote_tag_status=0; \
	git ls-remote --exit-code --tags origin "refs/tags/$(RELEASE_TAG)" >/dev/null 2>&1 || remote_tag_status=$$?; \
	if [ "$$remote_tag_status" -eq 2 ]; then \
		echo "Error: remote tag $(RELEASE_TAG) does not exist. No release was created." >&2; \
		exit 1; \
	elif [ "$$remote_tag_status" -ne 0 ]; then \
		echo "Error: unable to inspect remote tag $(RELEASE_TAG). No release was created." >&2; \
		exit 1; \
	fi
	@release_view=$$(gh release view "$(RELEASE_TAG)" 2>&1 >/dev/null); \
	release_status=$$?; \
	if [ "$$release_status" -eq 0 ]; then \
		echo "Error: GitHub release $(RELEASE_TAG) already exists. No release was created." >&2; \
		exit 1; \
	elif ! printf '%s\n' "$$release_view" | grep -Eiq 'not found|could not find'; then \
		echo "Error: unable to inspect GitHub release $(RELEASE_TAG): $$release_view" >&2; \
		echo "No release was created." >&2; \
		exit 1; \
	fi
	@set -e; \
	notes_file=$$(mktemp); \
	trap 'rm -f "$$notes_file"' EXIT; \
	$(MAKE) --no-print-directory release-notes VERSION='$(RELEASE_VERSION)' > "$$notes_file"; \
	gh release create "$(RELEASE_TAG)" --title "$(RELEASE_TAG)" --notes-file "$$notes_file" --verify-tag; \
	echo "Created GitHub release $(RELEASE_TAG) from existing remote tag."

release-publish: release-check
	@set -e; \
	notes_file=$$(mktemp); \
	trap 'rm -f "$$notes_file"' EXIT; \
	$(MAKE) --no-print-directory release-notes VERSION='$(RELEASE_VERSION)' > "$$notes_file"; \
	git tag -a "$(RELEASE_TAG)" -m "Release $(RELEASE_TAG)"; \
	git push origin "$(RELEASE_TAG)"; \
	gh release create "$(RELEASE_TAG)" --title "$(RELEASE_TAG)" --notes-file "$$notes_file"; \
	echo "Published GitHub release $(RELEASE_TAG)."
