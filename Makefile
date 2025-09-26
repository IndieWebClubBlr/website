# IndiewebClubBlr website generator
# Provides convenient commands for development and usage

.PHONY: help setup install clean run build assets serve watch clean_venv clean_cache clean_all
.DEFAULT_GOAL := help

# Variables
PYTHON := python3
VENV_DIR := venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_DIR)/bin/pip

# Targets
help:
	@echo "IndiewebClubBlr website generator"
	@echo "===================="
	@echo ""
	@echo "Available commands:"
	@echo "  make setup          - Set up virtual environment and install dependencies"
	@echo "  make install        - Install dependencies (assumes venv exists)"
	@echo "  make build          - Build the website (add CACHE=true to enable caching)"
	@echo "  make assets         - Copy assets to the build directory"
	@echo "  make clean          - Remove generated files"
	@echo "  make clean_venv     - Remove the virtual environment"
	@echo "  make clean_cache    - Remove the cache"
	@echo "  make clean_all      - Remove all generated files, virtual environment and cache"
	@echo "  make serve          - Serve the website on localhost"
	@echo "  make watch          - Watch for changes and rebuild the website/copy assets"
	@echo "  make help           - Show this help message"

# Set up virtual environment and install dependencies
setup: $(VENV_DIR)/bin/activate clean
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
	$(VENV_PYTHON) generator.py blogroll.opml _site $(if $(VERBOSE),--verbose) $(if $(CACHE),--cache)
	@echo "Generated website"

# Copy the assets
assets:
	@echo "Copying assets..."
	mkdir -p _site/
	cp *.css _site/
	@echo "Copied assets"

# Clean up generated files
clean:
	@echo "Cleaning up..."
	rm -rf _site || true
	rm -f *.pyc
	rm -rf __pycache__ || true
	@echo "Cleanup complete"

# Clean up virtual environment
clean_venv:
	@echo "Cleaning up virtual environment..."
	rm -rf $(VENV_DIR)

# Clean up cache
clean_cache:
	@echo "Cleaning up cache..."
	rm -rf .cache || true

# Clean up all generated files
clean_all: clean clean_venv clean_cache

# Serve the website
serve:
	python3 -m http.server -d ./_site/

# Watch for changes and copy assets
watch:
	git ls-files | entr -p -r ./watch.sh /_
