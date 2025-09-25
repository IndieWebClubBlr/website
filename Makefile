# IndiewebClubBlr website generator
# Provides convenient commands for development and usage

.PHONY: help setup install clean run

# Default Python command
PYTHON := python3
VENV_DIR := venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_DIR)/bin/pip

# Default target
help:
	@echo "IndiewebClubBlr website generator"
	@echo "===================="
	@echo ""
	@echo "Available commands:"
	@echo "  make setup     - Set up virtual environment and install dependencies"
	@echo "  make install   - Install dependencies (assumes venv exists)"
	@echo "  make build     - Build the website"
	@echo "  make clean     - Remove virtual environment and generated files"
	@echo "  make help      - Show this help message"

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
	$(VENV_PYTHON) generator.py blogroll.opml _site
	@echo "Generated website"

# Clean up generated files and virtual environment
clean:
	@echo "Cleaning up..."
	rm -rf _site || true
	rm -f *.pyc
	rm -rf __pycache__ || true
	@echo "Cleanup complete"

clean_venv:
	@echo "Cleaning up virtual environment..."
	rm -rf $(VENV_DIR)

clean_cache:
	@echo "Cleaning up cache..."
	rm -rf .cache || true

clean_all: clean clean_venv clean_cache

serve:
	python3 -m http.server -d ./_site/
