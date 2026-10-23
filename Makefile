# IndiewebClubBlr website generator
# Provides convenient commands for development and usage

.PHONY: help setup install clean run build assets serve watch clean_venv clean_cache clean_all graph check-tools
.DEFAULT_GOAL := help

# Variables
PYTHON := python3
VENV_DIR := venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_DIR)/bin/pip
# Engine-crate crates.io downloads land in the gitignored .cache/ so they
# persist across builds (see shell.nix, which does the same for nix-shell).
# An existing CARGO_HOME (e.g. a rustup install) is left alone.
REPO_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
export CARGO_HOME ?= $(REPO_DIR)/.cache/cargo

# Targets
help:
	@echo "IndiewebClubBlr website generator"
	@echo "===================="
	@echo ""
	@echo "Available commands:"
	@echo "  make setup          - Set up virtual environment and install dependencies"
	@echo "  make install        - Install dependencies (assumes venv exists)"
	@echo "  make check-tools    - Check that the search toolchain is available"
	@echo "  make build          - Build the website (add CACHE=true to read from cache only)"
	@echo "  make graph          - Generate build dependency graph (outputs build_deps.dot and .svg)"
	@echo "  make assets         - Copy assets to the build directory"
	@echo "  make clean          - Remove generated files"
	@echo "  make clean_venv     - Remove the virtual environment"
	@echo "  make clean_cache    - Remove the cache"
	@echo "  make clean_all      - Remove all generated files, virtual environment and cache"
	@echo "  make serve          - Serve the website on localhost"
	@echo "  make watch          - Watch for changes and rebuild the website/copy assets"
	@echo "  make help           - Show this help message"

# Check the toolchain the /search/ page needs: the tinysearch binary and
# cargo with the wasm32-unknown-unknown target. The nix shell (shell.nix)
# provides all of it; see the README for the non-nix setup.
check-tools:
	@command -v tinysearch >/dev/null 2>&1 || { \
		echo "tinysearch not found."; \
		echo "  nix:    use the nix shell (nix-shell)"; \
		echo "  other:  download it from https://github.com/tinysearch/tinysearch/releases"; \
		exit 1; \
	}
	@command -v cargo >/dev/null 2>&1 || { \
		echo "cargo not found."; \
		echo "  nix:    use the nix shell (nix-shell)"; \
		echo "  other:  install Rust from https://rustup.rs"; \
		exit 1; \
	}
	@ls "$$(rustc --print sysroot)/lib/rustlib/wasm32-unknown-unknown/lib" >/dev/null 2>&1 || { \
		echo "rustc has no wasm32-unknown-unknown standard library."; \
		echo "  nix:    use the nix shell (nix-shell)"; \
		echo "  other:  run: rustup target add wasm32-unknown-unknown"; \
		exit 1; \
	}
	@echo "Search toolchain OK (wasm-opt is optional: `command -v wasm-opt`)"

# Set up virtual environment and install dependencies
setup: $(VENV_DIR)/bin/activate
	@echo "Setup complete! Virtual environment ready."
	@echo "To activate: source $(VENV_DIR)/bin/activate"

$(VENV_DIR)/bin/activate: requirements.txt
	@echo "Creating virtual environment..."
	$(PYTHON) -m venv $(VENV_DIR)
	@echo "Installing dependencies..."
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements.txt
	@touch $(VENV_DIR)/bin/activate

# Install dependencies (assumes virtual environment exists)
install:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Virtual environment not found. Run 'make setup' first."; \
		exit 1; \
	fi
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements.txt

# Build the website
build: setup blogroll.opml
	@echo "Building website..."
	mkdir -p _site/
	$(VENV_PYTHON) src/generator.py blogroll.opml _site $(if $(VERBOSE),--verbose) $(if $(CACHE),--cache) $(if $(CACHE_FALLBACK),--cache-fallback)
	@echo "Generated website"

# Copy the assets
assets:
	@echo "Copying assets..."
	mkdir -p _site/
	cp assets/*.css _site/
	cp assets/*.svg _site/
	cp assets/*.png _site/
	cp assets/*.js _site/
	@echo "Copied assets"

# Clean up generated files
clean:
	@echo "Cleaning up..."
	rm -rf _site || true
	rm -f *.pyc
	rm -rf __pycache__ || true
	rm -rf src/__pycache__ || true
	@echo "Cleanup complete"

# Clean up virtual environment
clean_venv:
	@echo "Cleaning up virtual environment..."
	rm -rf $(VENV_DIR)

# Clean up cache
clean_cache:
	@echo "Cleaning up cache..."
	rm -rf .cache .cargo || true
	rm -rf "$(CARGO_HOME)/search-engine" || true

# Clean up all generated files
clean_all: clean clean_venv clean_cache

# Serve the website
serve:
	$(VENV_PYTHON) scripts/serve.py 8000 ./_site/

# Watch for changes and copy assets
watch:
	make build CACHE=true
	git ls-files | entr -p -r ./watch.sh /_

# Generate build dependency graph
graph:
	make build CACHE=true VERBOSE=true 2>&1 | python3 scripts/gen_build_graph.py
