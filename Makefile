PYTHON ?= python3
export PYTHONPATH := src
RELEASE_VERSION = $(patsubst v%,%,$(VERSION))
RELEASE_TAG = v$(RELEASE_VERSION)

.PHONY: check test security-check smoke check-gh-env release-notes release-check release-publish

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
	linode-image-lab capture-deploy --config examples/config/capture-deploy-smoke.toml --execute

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

release-publish: release-check
	@set -e; \
	notes_file=$$(mktemp); \
	trap 'rm -f "$$notes_file"' EXIT; \
	$(MAKE) --no-print-directory release-notes VERSION='$(RELEASE_VERSION)' > "$$notes_file"; \
	git tag -a "$(RELEASE_TAG)" -m "Release $(RELEASE_TAG)"; \
	git push origin "$(RELEASE_TAG)"; \
	gh release create "$(RELEASE_TAG)" --title "$(RELEASE_TAG)" --notes-file "$$notes_file"; \
	echo "Published GitHub release $(RELEASE_TAG)."
